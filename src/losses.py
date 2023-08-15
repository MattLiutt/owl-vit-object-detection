import torch
from torch import nn
from scipy.optimize import linear_sum_assignment
from torchvision.ops import box_area
import numpy as np
import torch.nn.functional as F
from src.matcher import HungarianMatcher, box_iou, generalized_box_iou


def cross_entropy(preds, targets, reduction="none"):
    log_softmax = nn.LogSoftmax(dim=-1)
    loss = (-targets * log_softmax(preds)).sum(1)
    if reduction == "none":
        return loss
    elif reduction == "mean":
        return loss.mean()


class PushPullLoss(torch.nn.Module):
    def __init__(self, n_classes, device):
        super().__init__()
        self.matcher = HungarianMatcher(n_classes)
        self.background_label = n_classes
        self.temperature = 0.1
        self.gain = 100
        self.device = device

    def loss_boxes(self, outputs, targets, indices, idx, num_boxes):
        src_boxes = outputs["pred_boxes"][idx]
        target_boxes = torch.cat(
            [t["boxes"][i] for t, (_, i) in zip(targets, indices)], dim=0
        )
        loss_bbox = torch.nn.functional.l1_loss(
            src_boxes, target_boxes, reduction="none"
        )
        metadata = {}
        loss_bbox = loss_bbox.sum() / num_boxes
        metadata["loss_bbox"] = loss_bbox.tolist()
        loss_giou = 1 - torch.diag(generalized_box_iou(src_boxes, target_boxes))
        loss_giou = loss_giou.sum() / num_boxes

        return loss_bbox, loss_giou

    def forward(self, inputs):
        batch_size = len(inputs)
        loss_bbox = torch.tensor(0.0, device=self.device)
        loss_giou = torch.tensor(0.0, device=self.device)
        _image_embeddings = []
        _target_classes = []
        for inp in inputs:
            (
                image_embeddings,
                text_embeddings,
                target_classes,
                predicted_boxes,
                target_boxes,
            ) = inp

            in_preds = {
                "pred_logits": image_embeddings,
                "pred_boxes": predicted_boxes,
            }

            in_targets = [
                {"labels": _labels, "boxes": _boxes}
                for _boxes, _labels in zip(target_boxes, target_classes)
            ]

            target_classes, indices, idx = self.matcher(in_preds, in_targets)

            _loss_bbox, _loss_giou = self.loss_boxes(
                in_preds,
                in_targets,
                indices,
                idx,
                num_boxes=sum(len(t["labels"]) for t in in_targets),
            )

            loss_bbox += _loss_bbox
            loss_giou += _loss_giou

            for box, label in zip(predicted_boxes[0], target_classes[0]):
                if label == self.background_label:
                    continue

                iou, _ = box_iou(box.unsqueeze(0), predicted_boxes.squeeze(0))
                idx = iou > 0.85
                target_classes[idx] = label.item()

            # CLIP-Style contrastive loss
            text_embeddings.squeeze_(0)
            image_embeddings.squeeze_(0)
            target_classes.squeeze_(0)
            for image_embedding, label in zip(image_embeddings, target_classes):
                if label == self.background_label:
                    continue
                _image_embeddings.append(image_embedding)
                _target_classes.append(label)

        # Class loss
        labels = torch.tensor(
            _target_classes, dtype=torch.float, device="cuda"
        ).unsqueeze(1)
        targets = -torch.clamp(torch.cdist(labels, labels), 0, 1) + 1
        print(targets)
        exit()
        # Class loss from here down
        image_embeddings = torch.stack(_image_embeddings)
        image_embeddings_norm = torch.nn.functional.normalize(
            image_embeddings, p=2, dim=-1
        )

        sims = torch.clamp(
            image_embeddings_norm @ image_embeddings_norm.t(), 0, 1
        )  # The job is to push apart the image sims

        # Modulate loss like this
        sims[targets == 1.0] = sims[targets == 1.0].pow(self.gain)
        sims[targets == 0.0] = sims[targets == 0.0].pow(1 / self.gain)
        loss = F.binary_cross_entropy(sims, targets, reduction="none")
        loss = (torch.pow(1 - torch.exp(-loss), 2) * loss).sum()

        losses = {
            "loss_ce": loss / (batch_size**2),
            "loss_bg": loss / (batch_size**2),
            "loss_bbox": loss_bbox / batch_size,
            "loss_giou": loss_giou / batch_size,
        }
        return losses, image_embeddings, labels
