import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import xavier_init

from mmdet.core import AnchorGenerator, anchor_target, multi_apply
from ..losses import smooth_l1_loss
from ..registry import HEADS
from .anchor_head import AnchorHead


# TODO: add loss evaluator for SSD
@HEADS.register_module
class SSDHead(AnchorHead):

    def __init__(self,
                 input_size=300,
                 num_classes=81,
                 in_channels=(512, 1024, 512, 256, 256, 256),
                 anchor_strides=(8, 16, 32, 64, 100, 300),
                 basesize_ratio_range=(0.1, 0.9),
                 anchor_ratios=([2], [2, 3], [2, 3], [2, 3], [2], [2]),
                 anchor_heights=[],
                 anchor_widths=[],
                 target_means=(.0, .0, .0, .0),
                 target_stds=(1.0, 1.0, 1.0, 1.0),
                 depthwise_heads=False,
                 depthwise_heads_activations='relu6',
                 loss_balancing=False):
        super(AnchorHead, self).__init__()
        self.input_size = input_size
        self.num_classes = num_classes
        self.in_channels = in_channels
        self.cls_out_channels = num_classes
        if len(anchor_heights):
            assert len(anchor_heights) == len(anchor_widths)
            num_anchors = [len(anc_conf) for anc_conf in anchor_heights]
        else:
            num_anchors = [len(ratios) * 2 + 2 for ratios in anchor_ratios]
        reg_convs = []
        cls_convs = []
        for i in range(len(in_channels)):
            if depthwise_heads:
                activation_class = {
                    'relu': nn.ReLU,
                    'relu6': nn.ReLU6,
                }[depthwise_heads_activations]

                reg_conv = nn.Sequential(
                    nn.Conv2d(in_channels[i], in_channels[i],
                              kernel_size=3, padding=1, groups=in_channels[i]),
                    nn.BatchNorm2d(in_channels[i]),
                    activation_class(inplace=True),
                    nn.Conv2d(in_channels[i], num_anchors[i] * 4,
                              kernel_size=1, padding=0))
                cls_conv = nn.Sequential(
                    nn.Conv2d(in_channels[i], in_channels[i],
                              kernel_size=3, padding=1, groups=in_channels[i]),
                    nn.BatchNorm2d(in_channels[i]),
                    activation_class(inplace=True),
                    nn.Conv2d(in_channels[i], num_anchors[i] * num_classes,
                              kernel_size=1, padding=0))
            else:
                reg_conv = nn.Conv2d(
                    in_channels[i],
                    num_anchors[i] * 4,
                    kernel_size=3,
                    padding=1)
                cls_conv = nn.Conv2d(
                    in_channels[i],
                    num_anchors[i] * num_classes,
                    kernel_size=3,
                    padding=1)
            reg_convs.append(reg_conv)
            cls_convs.append(cls_conv)
        self.reg_convs = nn.ModuleList(reg_convs)
        self.cls_convs = nn.ModuleList(cls_convs)

        self.anchor_generators = []
        self.anchor_strides = anchor_strides
        if len(anchor_heights):
            assert len(anchor_heights) == len(anchor_widths)
            for k in range(len(anchor_strides)):
                assert len(anchor_widths[i]) == len(anchor_heights[i])
                stride = anchor_strides[k]
                if isinstance(stride, tuple):
                    ctr = ((stride[0] - 1) / 2., (stride[1] - 1) / 2.)
                else:
                    ctr = ((stride - 1) / 2., (stride - 1) / 2.)
                anchor_generator = AnchorGenerator(
                    0, [], [], widths=anchor_widths[k],
                    heights=anchor_heights[k],
                    scale_major=False, ctr=ctr)
                self.anchor_generators.append(anchor_generator)
        else:
            min_ratio, max_ratio = basesize_ratio_range
            min_ratio = int(min_ratio * 100)
            max_ratio = int(max_ratio * 100)
            step = int(np.floor(max_ratio - min_ratio) /
                       (len(in_channels) - 2))
            min_sizes = []
            max_sizes = []
            for r in range(int(min_ratio), int(max_ratio) + 1, step):
                min_sizes.append(int(input_size * r / 100))
                max_sizes.append(int(input_size * (r + step) / 100))
            min_sizes.insert(0, int(input_size * basesize_ratio_range[0] / 2))
            max_sizes.insert(0, int(input_size * basesize_ratio_range[0]))
            for k in range(len(anchor_strides)):
                base_size = min_sizes[k]
                stride = anchor_strides[k]
                if isinstance(stride, tuple):
                    ctr = ((stride[0] - 1) / 2., (stride[1] - 1) / 2.)
                else:
                    ctr = ((stride - 1) / 2., (stride - 1) / 2.)
                scales = [1., np.sqrt(max_sizes[k] / min_sizes[k])]
                ratios = [1.]
                for r in anchor_ratios[k]:
                    ratios += [1 / r, r]  # 4 or 6 ratio
                anchor_generator = AnchorGenerator(
                    base_size, scales, ratios, scale_major=False, ctr=ctr)
                indices = list(range(len(ratios)))
                indices.insert(1, len(indices))
                anchor_generator.base_anchors = torch.index_select(
                    anchor_generator.base_anchors, 0,
                    torch.LongTensor(indices))
                self.anchor_generators.append(anchor_generator)

        self.target_means = target_means
        self.target_stds = target_stds
        self.use_sigmoid_cls = False
        self.cls_focal_loss = False
        self.fp16_enabled = False
        self.loss_balancing = loss_balancing
        if self.loss_balancing:
            self.loss_weights = torch.nn.Parameter(torch.FloatTensor(2))
            for i in range(2):
                self.loss_weights.data[i] = 0.

    def init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                xavier_init(m, distribution='uniform', bias=0)

    def forward(self, feats):
        cls_scores = []
        bbox_preds = []
        for feat, reg_conv, cls_conv in zip(feats, self.reg_convs,
                                            self.cls_convs):
            cls_scores.append(cls_conv(feat))
            bbox_preds.append(reg_conv(feat))
        return cls_scores, bbox_preds

    def loss_single(self, cls_score, bbox_pred, labels, label_weights,
                    bbox_targets, bbox_weights, num_total_samples, cfg):
        loss_cls_all = F.cross_entropy(
            cls_score, labels, reduction='none') * label_weights
        pos_inds = (labels > 0).nonzero().view(-1)
        neg_inds = (labels == 0).nonzero().view(-1)

        num_pos_samples = pos_inds.size(0)
        num_neg_samples = cfg.neg_pos_ratio * num_pos_samples
        if num_neg_samples > neg_inds.size(0):
            num_neg_samples = neg_inds.size(0)
        topk_loss_cls_neg, _ = loss_cls_all[neg_inds].topk(num_neg_samples)
        assert num_pos_samples > 0
        assert num_neg_samples > 0
        assert num_total_samples > 0
        loss_cls_pos = loss_cls_all[pos_inds].sum()
        loss_cls_neg = topk_loss_cls_neg.sum()
        loss_cls = (loss_cls_pos + loss_cls_neg) / num_total_samples

        loss_bbox = smooth_l1_loss(
            bbox_pred,
            bbox_targets,
            bbox_weights,
            beta=cfg.smoothl1_beta,
            avg_factor=num_total_samples)
        return loss_cls[None], loss_bbox

    def loss(self,
             cls_scores,
             bbox_preds,
             gt_bboxes,
             gt_labels,
             img_metas,
             cfg,
             gt_bboxes_ignore=None):
        featmap_sizes = [featmap.size()[-2:] for featmap in cls_scores]
        assert len(featmap_sizes) == len(self.anchor_generators)

        device = cls_scores[0].device

        anchor_list, valid_flag_list = self.get_anchors(
            featmap_sizes, img_metas, device=device)
        cls_reg_targets = anchor_target(
            anchor_list,
            valid_flag_list,
            gt_bboxes,
            img_metas,
            self.target_means,
            self.target_stds,
            cfg,
            gt_bboxes_ignore_list=gt_bboxes_ignore,
            gt_labels_list=gt_labels,
            label_channels=1,
            sampling=False,
            unmap_outputs=True)
        if cls_reg_targets is None:
            return None
        (labels_list, label_weights_list, bbox_targets_list, bbox_weights_list,
         num_total_pos, num_total_neg) = cls_reg_targets

        num_images = len(img_metas)
        all_cls_scores = torch.cat([
            s.permute(0, 2, 3, 1).reshape(
                num_images, -1, self.cls_out_channels) for s in cls_scores
        ], 1)
        all_labels = torch.cat(labels_list, -1).view(num_images, -1)
        all_label_weights = torch.cat(label_weights_list,
                                      -1).view(num_images, -1)
        all_bbox_preds = torch.cat([
            b.permute(0, 2, 3, 1).reshape(num_images, -1, 4)
            for b in bbox_preds
        ], -2)
        all_bbox_targets = torch.cat(bbox_targets_list,
                                     -2).view(num_images, -1, 4)
        all_bbox_weights = torch.cat(bbox_weights_list,
                                     -2).view(num_images, -1, 4)

        # check NaN and Inf
        assert torch.isfinite(all_cls_scores).all().item(), \
            'classification scores become infinite or NaN!'
        assert torch.isfinite(all_bbox_preds).all().item(), \
            'bbox predications become infinite or NaN!'

        losses_cls, losses_bbox = multi_apply(
            self.loss_single,
            all_cls_scores,
            all_bbox_preds,
            all_labels,
            all_label_weights,
            all_bbox_targets,
            all_bbox_weights,
            num_total_samples=num_total_pos,
            cfg=cfg)

        if self.loss_balancing:
            losses_cls, losses_reg = self._balance_losses(losses_cls,
                                                          losses_bbox)

        return dict(loss_cls=losses_cls, loss_bbox=losses_bbox)

    def _balance_losses(self, losses_cls, losses_reg):
        loss_cls = sum(_loss.mean() for _loss in losses_cls)
        loss_cls = torch.exp(-self.loss_weights[0])*loss_cls + \
            0.5*self.loss_weights[0]

        loss_reg = sum(_loss.mean() for _loss in losses_reg)
        loss_reg = torch.exp(-self.loss_weights[1])*loss_reg + \
            0.5*self.loss_weights[1]

        return (loss_cls, loss_reg)
