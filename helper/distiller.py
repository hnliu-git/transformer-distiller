"""
Distillers for knowledge distillation on different layers
"""

import os
import wandb
import torch
import torchmetrics

from argparse import ArgumentParser
from typing import Any, Dict, Optional
from pytorch_lightning import LightningModule
from pytorch_lightning.plugins import CheckpointIO
from pytorch_lightning.utilities.types import _PATH
from transformers import get_linear_schedule_with_warmup
from pytorch_lightning.utilities.cloud_io import get_filesystem


class HgCkptIO(CheckpointIO):

    def save_checkpoint(self, checkpoint: Dict[str, Any], path: _PATH, storage_options: Optional[Any] = None) -> None:
        '''Save the fine-tuned model in a hugging-face style.

        Args:
            checkpoint: ckpt, but only key 'hg_model' matters
            path: path to save the ckpt
            storage_options: not used
        '''
        fs = get_filesystem(path)
        fs.makedirs(os.path.dirname(path), exist_ok=True)
        checkpoint['student'].save_pretrained(path)

    def load_checkpoint(self, path: _PATH, storage_options: Optional[Any] = None) -> Dict[str, Any]:
        pass

    def remove_checkpoint(self, path: _PATH) -> None:
        """Remove checkpoint file from the filesystem.

        Args:
            path: Path to checkpoint
        """
        fs = get_filesystem(path)
        if fs.exists(path):
            fs.rm(path, recursive=True)


class BaseDistiller(LightningModule):
    """
    ====================================
        A distiller for all layers
    ====================================
    """
    @staticmethod
    def add_model_specific_args(parent_parser):
        """"""
        parser = ArgumentParser(parents=[parent_parser], add_help=False)

        # Training Configurations
        parser.add_argument("--weight_decay", default=5e-5, type=float)
        parser.add_argument("--epochs", default=5, type=int)
        parser.add_argument("--learning_rate", default=1e-4, type=float)
        parser.add_argument("--eps", default=1e-8, type=float)
        parser.add_argument("--num_classes", default=2, type=int)
        parser.add_argument("--plot_attentions", default=True, type=bool)

        # Distillation Configurations
        parser.add_argument("--temperature", default=4, type=float)
        parser.add_argument("--flood", default=0.07, type=float)

        return parser

    def __init__(self, teacher, student, args, adaptors):
        super().__init__()

        self.save_hyperparameters(args)

        self.teacher = teacher
        self.student = student
        self.adaptors = adaptors

        # Metrics
        self.acc_s = torchmetrics.Accuracy(num_classes=args.num_classes)
        self.f1_s = torchmetrics.F1Score(num_classes=args.num_classes)

        self.test_acc = torchmetrics.Accuracy(num_classes=args.num_classes)
        self.test_f1 = torchmetrics.F1Score(num_classes=args.num_classes)

    def compute_loss(self, out_t, out_s, mask=None):
        loss_dict = {
            'pred:nll': out_s.get('loss', 0),
            'nll_loss_teacher': out_t.get('loss', 0)
        }

        for adaptor in self.adaptors:
            feature_name = adaptor.name.split(':')[0]
            loss_dict[adaptor.name] = adaptor.w * adaptor(out_t.get(feature_name),
                                                          out_s.get(feature_name),
                                                          mask=mask)

        return loss_dict

    def forward(self, batch):
        self.teacher.eval()
        with torch.no_grad():
            teacher_out = self.teacher(**batch)

        student_out = self.student(**batch)

        return teacher_out, student_out

    def configure_optimizers(self):
        no_decay = ["bias", "LayerNorm.weight"]

        parameters = [(n, p) for n, p in self.named_parameters() if 'teacher' not in n]

        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in parameters if not any(nd in n for nd in no_decay)],
                "weight_decay": self.hparams.weight_decay,
            },
            {
                "params": [p for n, p in parameters if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]

        optimizer = torch.optim.AdamW(optimizer_grouped_parameters,
                                      lr=self.hparams.learning_rate,
                                      eps=self.hparams.eps, )

        scheduler = get_linear_schedule_with_warmup(
            optimizer,
            num_training_steps=self.hparams.num_training_steps,
            num_warmup_steps=self.hparams.num_warmup_steps
        )

        return [optimizer], [{"scheduler": scheduler, "interval": "step"}]

    def training_step(self, batch, idx):
        loss_dict = self.compute_loss(
            *self(batch),
            batch.get('attention_mask')
        )

        for k, v in loss_dict.items():
            self.log(k, v, on_step=True, on_epoch=False, prog_bar=True, logger=True)

        if loss_dict['pred:nll'] == 0:
            loss_dict.pop('pred:nll')
            loss_dict.pop('nll_loss_teacher')
        # else:
            # Flooding: method to avoid over-fit, check https://arxiv.org/pdf/2002.08709.pdf
            # loss_dict['pred:nll'] = torch.abs(loss_dict['pred:nll'] - self.hparams.flood) + self.hparams.flood

        if 'nll_loss_teacher' in loss_dict:
            loss_dict.pop('nll_loss_teacher')

        return sum(loss_dict.values())

    def validation_step(self, batch, idx):
        labels = batch['labels']
        out_t, out_s = self(batch)
        pred_s = torch.argmax(out_s.logits, dim=1)

        self.f1_s(pred_s, labels)
        self.acc_s(pred_s, labels)

        # Plot attention matrices of the first item in the first batch
        if self.hparams.plot_attentions and idx == 0:
            attn_t = out_t.attentions
            attn_s = out_s.attentions
            mask = batch['attention_mask']

            first_zero_index = 0
            for index, item in enumerate(mask[0]):
                if item == 0:
                    first_zero_index = index
                    break

            # Unmasked text
            axis = [i for i in range(first_zero_index)]

            wandb.log({'%d-attn_t[-1]' % self.current_epoch: wandb.plots.HeatMap(axis, axis,
                                                                                 attn_t[-1][0, 0, :, :]
                                                                                 .detach().cpu(),
                                                                                 show_text=False)})

            wandb.log({'%d-attn_s[-1]' % self.current_epoch: wandb.plots.HeatMap(axis, axis,
                                                                                 attn_s[-1][0, 0, :, :]
                                                                                 .detach().cpu(),
                                                                                 show_text=False)})

        return {'val_loss': out_s.loss}

    def test_step(self, batch, idx):
        labels = batch['labels']
        _, out_s = self(batch)
        pred_s = torch.argmax(out_s.logits, dim=1)

        return {'test_acc': self.test_acc(pred_s, labels),
                'test_f1': self.test_f1(pred_s, labels)}

    def test_epoch_end(self, outputs) -> None:
        test_acc = torch.stack([x["test_acc"] for x in outputs]).mean()
        test_f1 = torch.stack([x["test_f1"] for x in outputs]).mean()
        self.log("test_acc", test_acc, prog_bar=True, logger=True)
        self.log("test_f1", test_f1, prog_bar=True, logger=True)

    def predict_step(self, batch, idx):
        batch.pop('labels')
        out_s = self.student(**batch)
        pred_s = torch.argmax(out_s.logits, dim=1)

        return pred_s

    def validation_epoch_end(self, outputs) -> None:
        val_loss = torch.stack([x["val_loss"] for x in outputs]).mean()
        self.log("val_loss", val_loss, prog_bar=True, logger=True)
        self.log('val_f1', self.f1_s)
        self.log('val_acc', self.acc_s)

    def on_save_checkpoint(self, checkpoint) -> None:
        """
            For the customed CheckpointIO
        """
        checkpoint['student'] = self.student


class InterDistiller(BaseDistiller):
    """
        A distiller for inter layer
        Archived by removing pred layer losses
    """
    def __init__(self, teacher, student, args, adaptors):
        super().__init__(teacher, student, args, adaptors)

    def training_step(self, batch, idx):
        '''
            Rewrite training_step and remove prediction layer losses
        '''
        loss_dict = self.compute_loss(
            *self(batch),
            batch.get('attention_mask')
        )

        for k, v in loss_dict.items():
            self.log(k, v, on_step=True, on_epoch=False, prog_bar=True, logger=True)

        if loss_dict['pred:nll'] == 0:
            loss_dict.pop('pred:nll')
            loss_dict.pop('nll_loss_teacher')
        elif 'pred:nll' in loss_dict:
            loss_dict.pop('pred:nll')

        if 'nll_loss_teacher' in loss_dict:
            loss_dict.pop('nll_loss_teacher')

        return sum(loss_dict.values())


class PredDistiller(BaseDistiller):
    """
        A distiller for pred layer
        Archived by fixing inter layer weights
    """

    def __init__(self, teacher, student, args, adaptors):
        super().__init__(teacher, student, args, adaptors)

    def configure_optimizers(self):

        no_decay = ["bias", "LayerNorm.weight"]

        parameters = [(n, p) for n, p in self.named_parameters() if 'classifier' in n]

        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in parameters if not any(nd in n for nd in no_decay)],
                "weight_decay": self.hparams.weight_decay,
            },
            {
                "params": [p for n, p in parameters if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]

        optimizer = torch.optim.AdamW(optimizer_grouped_parameters,
                                      lr=self.hparams.learning_rate,
                                      eps=self.hparams.eps, )

        scheduler = get_linear_schedule_with_warmup(
            optimizer,
            num_training_steps=self.hparams.num_training_steps,
            num_warmup_steps=self.hparams.num_warmup_steps
        )

        return [optimizer], [{"scheduler": scheduler, "interval": "step"}]
