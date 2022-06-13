# Copyright (c) OpenMMLab. All rights reserved.
import os.path as osp
from itertools import filterfalse, groupby
from typing import (Any, Callable, Dict, Iterable, List, Optional, Sequence,
                    Union)

import numpy as np
import xtcocotools.mask as cocomask
from mmcv.fileio import load
from mmengine.utils import check_file_exist, is_list_of
from xtcocotools.coco import COCO

from mmpose.registry import DATASETS
from ..base import BasePoseDataset


@DATASETS.register_module()
class CocoDataset(BasePoseDataset):
    """COCO dataset for pose estimation.

    "Microsoft COCO: Common Objects in Context", ECCV'2014.
    More details can be found in the `paper
    <https://arxiv.org/abs/1405.0312>`__ .

    COCO keypoints::

        0: 'nose',
        1: 'left_eye',
        2: 'right_eye',
        3: 'left_ear',
        4: 'right_ear',
        5: 'left_shoulder',
        6: 'right_shoulder',
        7: 'left_elbow',
        8: 'right_elbow',
        9: 'left_wrist',
        10: 'right_wrist',
        11: 'left_hip',
        12: 'right_hip',
        13: 'left_knee',
        14: 'right_knee',
        15: 'left_ankle',
        16: 'right_ankle'

    Args:
        ann_file (str): Annotation file path. Defaults to ''.
        bbox_file (str, optional): Detection result file path. If
            ``bbox_file`` is set, detected bboxes loaded from this file will
            be used instead of ground-truth bboxes. This setting is only for
            evaluation, i.e., ignored when ``test_mode`` is ``False``.
            Defaults to None.
        data_mode (str): Specifies the mode of data samples: ``'topdown'`` or
            ``'bottomup'``. In ``'topdown'`` mode, each data sample contains
            one instance; while in ``'bottomup'`` mode, each data sample
            contains all instances in a image. Defaults to ``'topdown'``
        metainfo (dict, optional): Meta information for dataset, such as class
            information. Defaults to None.
        data_root (str, optional): The root directory for ``data_prefix`` and
            ``ann_file``. Defaults to None.
        data_prefix (dict, optional): Prefix for training data. Defaults to
            dict(img=None, ann=None).
        filter_cfg (dict, optional): Config for filter data. Defaults to None.
        indices (int or Sequence[int], optional): Support using first few
            data in annotation file to facilitate training/testing on a smaller
            dataset. Defaults to None which means using all ``data_infos``.
        serialize_data (bool, optional): Whether to hold memory using
            serialized objects, when enabled, data loader workers can use
            shared RAM from master process instead of making a copy. Defaults
            to True.
        pipeline (list, optional): Processing pipeline. Defaults to [].
        test_mode (bool, optional): ``test_mode=True`` means in test phase.
            Defaults to False.
        lazy_init (bool, optional): Whether to load annotation during
            instantiation. In some cases, such as visualization, only the meta
            information of the dataset is needed, which is not necessary to
            load annotation file. ``Basedataset`` can skip load annotations to
            save time by set ``lazy_init=False``. Defaults to False.
        max_refetch (int, optional): If ``Basedataset.prepare_data`` get a
            None img. The maximum extra number of cycles to get a valid
            image. Defaults to 1000.
    """

    METAINFO: dict = dict(from_config='configs/_base_/datasets/coco.py')

    def __init__(self,
                 ann_file: str = '',
                 bbox_file: Optional[str] = None,
                 data_mode: str = 'topdown',
                 metainfo: Optional[dict] = None,
                 data_root: Optional[str] = None,
                 data_prefix: dict = dict(img=None, ann=None),
                 filter_cfg: Optional[dict] = None,
                 indices: Optional[Union[int, Sequence[int]]] = None,
                 serialize_data: bool = True,
                 pipeline: List[Union[dict, Callable]] = [],
                 test_mode: bool = False,
                 lazy_init: bool = False,
                 max_refetch: int = 1000):

        if data_mode not in {'topdown', 'bottomup'}:
            raise ValueError(
                f'{self.__class__.__name__} got invalid data_mode: '
                f'{data_mode}. Should be "topdown" or "bottomup".')
        self.data_mode = data_mode

        if bbox_file:
            if self.data_mode != 'topdown':
                raise ValueError(
                    f'{self.__class__.__name__} is set to {self.data_mode}: '
                    'mode, while "bbox_file" is only supported in '
                    'topdown mode.')

            if not test_mode:
                raise ValueError(
                    f'{self.__class__.__name__} has `test_mode==False` '
                    'while "bbox_file" is only supported when '
                    '`test_mode==True`.')
        self.bbox_file = bbox_file

        super().__init__(
            ann_file=ann_file,
            metainfo=metainfo,
            data_root=data_root,
            data_prefix=data_prefix,
            filter_cfg=filter_cfg,
            indices=indices,
            serialize_data=serialize_data,
            pipeline=pipeline,
            test_mode=test_mode,
            lazy_init=lazy_init,
            max_refetch=max_refetch)

    def load_data_list(self) -> List[dict]:
        """Load data list from COCO annotation file or person detection result
        file."""

        if self.bbox_file:
            data_list = self._load_detection_results()
        else:
            data_list = self._load_annotations()

            if self.data_mode == 'topdown':
                data_list = self._get_topdown_data_infos(data_list)
            else:
                data_list = self._get_bottomup_data_infos(data_list)

        return data_list

    def _load_annotations(self):
        """Load data from annotations in COCO format."""

        check_file_exist(self.ann_file)

        coco = COCO(self.ann_file)
        data_list = []

        for img_id in coco.getImgIds():
            img = coco.loadImgs(img_id)[0]
            ann_ids = coco.getAnnIds(imgIds=img_id, iscrowd=False)
            for ann in coco.loadAnns(ann_ids):

                data_info = self.parse_data_info(
                    dict(raw_ann_info=ann, raw_img_info=img))

                # skip invalid instance annotation.
                if not data_info:
                    continue

                data_list.append(data_info)

        return data_list

    def parse_data_info(self, raw_data_info: dict) -> Optional[dict]:
        """Parse raw COCO annotation of an instance.

        Args:
            raw_data_info (dict): Raw data information loaded from
                ``ann_file``. It should have following contents:

                - ``'raw_ann_info'``: Raw annotation of an instance
                - ``'raw_img_info'``: Raw information of the image that
                    contains the instance

        Returns:
            dict: Parsed instance annotation
        """

        ann = raw_data_info['raw_ann_info']
        img = raw_data_info['raw_img_info']

        image_file = osp.join(self.img_prefix, img['file_name'])
        img_w, img_h = img['width'], img['height']

        # get bbox in shape [1, 4], formatted as xywh
        x, y, w, h = ann['bbox']
        x1 = np.clip(x, 0, img_w - 1)
        y1 = np.clip(y, 0, img_h - 1)
        x2 = np.clip(x + w, 0, img_w - 1)
        y2 = np.clip(y + h, 0, img_h - 1)

        bbox = np.array([x1, y1, x2 - x1, y2 - y1],
                        dtype=np.float32).reshape(1, 4)

        # keypoints in shape [1, K, 2] and keypoints_visible in [1, K, 1]
        _keypoints = np.array(
            ann['keypoints'], dtype=np.float32).reshape(1, -1, 3)
        keypoints = _keypoints[..., :2]
        keypoints_visible = np.minimum(1, _keypoints[..., 2:3])

        if 'num_keypoints' in ann:
            num_keypoints = ann['num_keypoints']
        else:
            num_keypoints = np.count_nonzero(keypoints.max(axis=2))

        data_info = {
            'image_id': ann['image_id'],
            'image_file': image_file,
            'image_shape': (img_h, img_w, 3),
            'bbox': bbox,
            'bbox_score': np.ones(1, dtype=np.float32),
            'num_keypoints': num_keypoints,
            'keypoints': keypoints,
            'keypoints_visible': keypoints_visible,
            'iscrowd': ann.get('iscrowd', 0),
            'segmentation': ann.get('segmentation', None),
            'id': ann['id'],
            'sigmas': self.metainfo['sigmas']
        }

        return data_info

    @staticmethod
    def _is_valid_instance(data_info: Dict) -> bool:
        """Check a data info is an instance with valid bbox and keypoint
        annotations."""
        # crowd annotation
        if data_info['iscrowd']:
            return False
        # invalid keypoints
        if data_info['num_keypoints'] == 0:
            return False
        # invalid bbox
        w, h = data_info['bbox'][0, 2:4]
        if w <= 0 or h <= 0:
            return False
        return True

    def _get_topdown_data_infos(self, data_list: List[Dict]) -> List[Dict]:
        """Organize the data list in top-down mode."""
        # sanitize data samples
        data_list_tp = list(filter(self._is_valid_instance, data_list))

        return data_list_tp

    def _get_bottomup_data_infos(self, data_list):
        """Organize the data list in bottom-up mode."""

        def _concat(seq: Iterable, key: Any, axis=0):
            seq = [x[key] for x in seq]
            if isinstance(seq[0], np.ndarray):
                seq = np.concatenate(seq, axis=axis)
            return seq

        # bottom-up data list
        data_list_bu = []

        # group instances by image_file
        for image_id, data_infos in groupby(data_list,
                                            lambda x: x['image_id']):
            data_infos = list(data_infos)

            # get valid instances for keypoint annotations
            data_infos_valid = list(
                filter(self._is_valid_instance, data_infos))
            if not data_infos_valid:
                continue

            image_file = data_infos_valid[0]['image_file']
            image_shape = data_infos_valid[0]['image_shape']
            sigmas = data_infos_valid[0]['sigmas']

            # image data
            data_info_bu = {
                'image_id': image_id,
                'image_file': image_file,
                'image_shape': image_shape,
                'sigmas': sigmas
            }
            # instance data
            for key in data_infos_valid[0].keys():
                if key not in data_info_bu:
                    data_info_bu[key] = _concat(data_infos_valid, key)

            # get region mask of invalid instances (crowd objects or objects
            # without valid keypoint annotations)

            # RLE is a simple yet efficient format for storing binary masks.
            # details can be found at `COCO tools <https://github.com/
            # cocodataset/cocoapi/blob/master/PythonAPI/pycocotools/
            # mask.py>`__
            rles = []
            for data_info_invalid in filterfalse(self._is_valid_instance,
                                                 data_infos):
                seg = data_info_invalid.get('segmentation', None)
                if not seg:
                    continue

                img_h, img_w = image_shape[:2]

                if data_info_invalid['iscrowd']:
                    # crowd object has a unitary mask region
                    rles.append(cocomask.frPyObjects(seg, img_h, img_w))
                elif data_info_invalid['num_keypoints'] == 0:
                    # non-crowd object has a list of mask regions
                    rles.extend(cocomask.frPyObjects(seg, img_h, img_w))

            data_info_bu['mask_invalid_rle'] = cocomask.merge(rles)

            data_list_bu.append(data_info_bu)

        return data_list_bu

    def _load_detection_results(self) -> List[dict]:
        """Load data from detection results with dummy keypoint annotations."""

        check_file_exist(self.ann_file)
        check_file_exist(self.bbox_file)

        # load detection results
        det_results = load(self.bbox_file)
        assert is_list_of(det_results, dict)

        # load coco annotations to build image id-to-name index
        coco = COCO(self.ann_file)

        num_keypoints = self.metainfo['num_keypoints']
        data_list = []
        id_ = 0
        for det in det_results:
            # remove non-human instances
            if det['category_id'] != 1:
                continue

            img = coco.loadImgs(det['image_id'])[0]

            image_file = osp.join(self.img_prefix, img['file_name'])
            bbox = np.array(det['bbox'][:4], dtype=np.float32).reshape(1, 4)
            bbox_score = np.array(det['score'], dtype=np.float32).reshape(1)

            # use dummy keypoint location and visibility
            keypoints = np.zeros((1, num_keypoints, 2), dtype=np.float32)
            keypoints_visible = np.ones((1, num_keypoints, 1),
                                        dtype=np.float32)

            data_list.append({
                'image_id': det['image_id'],
                'image_file': image_file,
                'image_shape': (img['height'], img['width'], 3),
                'bbox': bbox,
                'bbox_score': bbox_score,
                'keypoints': keypoints,
                'keypoints_visible': keypoints_visible,
                'id': id_,
            })

            id_ += 1

        return data_list

    def filter_data(self) -> List[dict]:
        """Filter annotations according to filter_cfg. Defaults return full
        ``data_list``.

        If 'bbox_score_thr` in filter_cfg, the annotation with bbox_score below
        the threshold `bbox_thr` will be filtered out.
        """

        data_list = self.data_list

        if self.filter_cfg is None:
            return data_list

        # filter out annotations with a bbox_score below the threshold
        if 'bbox_score_thr' in self.filter_cfg:

            if self.data_mode != 'topdown':
                raise ValueError(
                    f'{self.__class__.__name__} is set to {self.data_mode} '
                    'mode, while "bbox_score_thr" is only supported in '
                    'topdown mode.')

            thr = self.filter_cfg['bbox_score_thr']
            data_list = list(
                filterfalse(lambda ann: ann['bbox_score'] < thr, data_list))

        return data_list