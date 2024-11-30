import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.models import resnet50

# import random
# import numpy as np

# from collections import OrderedDict
from torchmeta.modules import MetaSequential, MetaLinear

from metamodules import FCBlock, BatchLinear, HyperNetwork, get_subdict
from torchmeta.modules import MetaModule

from backbones import ResNet50, MobileNetV2, EfficientNetB0

backbone_dict = {
    'resnet50': ResNet50,
    'mobilenetv2': MobileNetV2,
    'efficientnetb0': EfficientNetB0
}

class HyperCMTL(nn.Module):
    """
    Hypernetwork-based Conditional Multi-Task Learning (HyperCMTL) model.

    This model combines a convolutional backbone, a task-specific head, and a hypernetwork
    to dynamically generate parameters for task-specific learning. It is designed for
    applications requiring task conditioning, such as meta-learning or multi-task learning.

    Args:
        num_instances (int): Number of task instances to support (e.g., number of tasks).
        device (str, optional): Device for computation ('cuda' or 'cpu'). Default is 'cuda'.
        std (float, optional): Standard deviation for initializing the task embeddings. Default is 0.01.

    Attributes:
        num_instances (int): Number of task instances.
        device (torch.device): Device for computation.
        std (float): Standard deviation for embedding initialization.
        backbone (ConvBackbone): Convolutional network for feature extraction.
        task_head (TaskHead): Fully connected network for task-specific classification.
        hypernet (HyperNetwork): Hypernetwork to generate parameters for the task head.
        hyper_emb (nn.Embedding): Task-specific embeddings used as input to the hypernetwork.
    """
    def __init__(self,
                 num_instances=1,
                 backbone='resnet50',  # Backbone architecture
                 task_head_projection_size=64,             # Task head hidden layer size
                 task_head_num_classes=2,                  # Task head output size
                 hyper_hidden_features=256,                # Hypernetwork hidden layer size
                 hyper_hidden_layers=2,                    # Hypernetwork number of layers
                 device='cuda',
                 channels=1,
                 img_size=[32, 32],
                 std=0.01):
        super().__init__()

        self.num_instances = num_instances
        self.backbone = backbone
        self.task_head_projection_size = task_head_projection_size
        self.task_head_num_classes = task_head_num_classes
        self.hyper_hidden_features = hyper_hidden_features
        self.hyper_hidden_layers = hyper_hidden_layers
        self.device = device
        self.channels = channels
        self.std = std

        # Backbone
        '''self.backbone = ConvBackbone(layers=backbone_layers,
                                     input_size=(channels, img_size[0], img_size[1]),
                                     device=device)
        '''
        if backbone in backbone_dict:
            self.backbone = backbone_dict[backbone](device=device, pretrained=True)
        else: 
            raise ValueError(f"Backbone {backbone} is not supported.")
        # Task head
        self.task_head = TaskHead(input_size=self.backbone.num_features,
                                  projection_size=task_head_projection_size,
                                  num_classes=task_head_num_classes,
                                  dropout=0.5,
                                  device=device)

        # Hypernetwork
        hn_in = 64  # Input size for hypernetwork embedding
        self.hypernet = HyperNetwork(hyper_in_features=hn_in,
                                     hyper_hidden_layers=hyper_hidden_layers,
                                     hyper_hidden_features=hyper_hidden_features,
                                     hypo_module=self.task_head,
                                     activation='relu')

        self.hyper_emb = nn.Embedding(self.num_instances, hn_in)
        nn.init.normal_(self.hyper_emb.weight, mean=0, std=std)

    def get_params(self, task_idx):
        z = self.hyper_emb(torch.LongTensor([task_idx]).to(self.device))
        return self.hypernet(z)


    def forward(self, support_set, task_idx, **kwargs):
        params = self.get_params(task_idx)
        # print("after get params", params)
        backbone_out = self.backbone(support_set)
        task_head_out = self.task_head(backbone_out, params=params)
        
        return task_head_out.squeeze(0)
    
    def deepcopy(self, device='cuda'):
        new_model = HyperCMTL(
            num_instances=self.num_instances,
            #backbone_layers=self.backbone_layers,
            task_head_projection_size=self.task_head_projection_size,
            task_head_num_classes=self.task_head_num_classes,
            hyper_hidden_features=self.hyper_hidden_features,
            hyper_hidden_layers=self.hyper_hidden_layers,
            device=device,
            channels=self.channels,
            std=0.01
        ).to(device)
        new_model.load_state_dict(self.state_dict())
        return new_model.to(device)
    
    def get_optimizer_list(self):
        # networks = [self.backbone, self.task_head, self.hypernet, self.hyper_emb]
        optimizer_list = []
        optimizer_list.append({'params': self.hyper_emb.parameters(), 'lr': 1e-3})
        optimizer_list.extend(self.hypernet.get_optimizer_list())
        optimizer_list.extend(self.backbone.get_optimizer_list())
        optimizer_list.extend(self.task_head.get_optimizer_list())
        print("optimizer_list", optimizer_list)
        return optimizer_list

class TaskHead(MetaModule):
    def __init__(self, input_size: int, # number of features in the backbone's output
                 projection_size: int,  # number of neurons in the hidden layer
                 num_classes: int,      # number of output neurons
                 dropout: float=0.,     # optional dropout rate to apply
                 device="cuda"):
        super().__init__()

        self.projection = BatchLinear(input_size, projection_size)
        self.classifier = BatchLinear(projection_size, num_classes)

        if dropout > 0:
            self.dropout = nn.Dropout(dropout)
        else:
            self.dropout = nn.Identity()

        self.relu = nn.ReLU()

        self.device = device
        self.to(device)

    def forward(self, x, params):
        # assume x is already unactivated feature logits,
        # e.g. from resnet backbone
        # print("inside taskhead forward", params)
        # print("after get_subdict", get_subdict(params, 'projection'))
        x = self.projection(self.relu(self.dropout(x)), params=get_subdict(params, 'projection'))
        x = self.classifier(self.relu(self.dropout(x)), params=get_subdict(params, 'classifier'))

        return x
    
    def get_optimizer_list(self):
        optimizer_list = [{'params': self.parameters(), 'lr': 1e-3}]
        return optimizer_list


class MultitaskModel(nn.Module):
    def __init__(self, backbone: nn.Module,
                 device="cuda"):
        super().__init__()

        self.backbone = backbone

        # a dict mapping task IDs to the classification heads for those tasks:
        self.task_heads = nn.ModuleDict()
        # we must use a nn.ModuleDict instead of a base python dict,
        # to ensure that the modules inside are properly registered in self.parameters() etc.

        self.relu = nn.ReLU()
        self.device = device
        self.to(device)

    def forward(self,
                x: torch.Tensor,
                task_id: int):

        task_id = str(int(task_id))
        # nn.ModuleDict requires string keys for some reason,
        # so we have to be sure to cast the task_id from tensor(2) to 2 to '2'

        assert task_id in self.task_heads, f"no head exists for task id {task_id}"

        # select which classifier head to use:
        chosen_head = self.task_heads[task_id]

        # activated features from backbone:
        x = self.relu(self.backbone(x))
        # task-specific prediction:
        x = chosen_head(x)

        return x

    def add_task(self,
                 task_id: int,
                 head: nn.Module):
        """accepts an integer task_id and a classification head
        associated to that task.
        adds the head to this model's collection of task heads."""
        self.task_heads[str(task_id)] = head

    @property
    def num_task_heads(self):
        return len(self.task_heads)
    
    