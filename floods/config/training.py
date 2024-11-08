import hashlib
from typing import Any, List, Optional

from inplace_abn import InPlaceABN, InPlaceABNSync
from pydantic import BaseSettings, Field, validator
from torch.nn import BatchNorm2d, Identity, LeakyReLU, ReLU
from torch.optim import SGD, Adam, AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR, ExponentialLR, ReduceLROnPlateau

from floods.config.base import CallableEnum, EnvConfig, Initializer, InstantiableSettings
from floods.losses import BCEWithLogitsLoss, CombinedLoss, FocalLoss, FocalTverskyLoss, LovaszSoftmax
from floods.metrics import F1Score, IoU, Precision, Recall
from floods.utils.schedulers import PolynomialLRDecay


class Optimizers(CallableEnum):
    adam = Initializer(Adam)
    adamw = Initializer(AdamW)
    sgd = Initializer(SGD, momentum=0.9)


class Schedulers(CallableEnum):
    plateau = Initializer(ReduceLROnPlateau)
    exp = Initializer(ExponentialLR, gamma=0.96)
    cosine = Initializer(CosineAnnealingLR, T_max=10)
    poly = Initializer(PolynomialLRDecay, max_decay_steps=99, end_learning_rate=0.0001, power=3.0)


class Losses(CallableEnum):
    bce = BCEWithLogitsLoss
    focal = FocalLoss
    tversky = FocalTverskyLoss
    lovasz = LovaszSoftmax
    combo = Initializer(CombinedLoss,
                        criterion_a=Initializer(BCEWithLogitsLoss),
                        criterion_b=Initializer(FocalTverskyLoss))


class Metrics(CallableEnum):
    f1 = Initializer(F1Score, ignore_index=255)
    iou = Initializer(IoU, ignore_index=255)
    precision = Initializer(Precision, ignore_index=255, reduction="macro")
    recall = Initializer(Recall, ignore_index=255, reduction="macro")


class NormLayers(CallableEnum):
    std = Initializer(BatchNorm2d)
    iabn = Initializer(InPlaceABN, activation="leaky_relu", activation_param=0.01)
    iabn_sync = Initializer(InPlaceABNSync, activation="leaky_relu", activation_param=0.01)


class ActivationLayers(CallableEnum):
    ident = Initializer(Identity)
    relu = Initializer(ReLU, inplace=True)
    lrelu = Initializer(LeakyReLU, inplace=True)


class TrainerConfig(BaseSettings):
    cpu: bool = Field(False, description="Whether to use CPU or not")
    amp: bool = Field(True, description="Whether to use mixed precision (native)")
    batch_size: int = Field(8, description="Batch size for training")
    num_workers: int = Field(4, description="Number of workers per dataloader")
    max_epochs: int = Field(100, description="How many epochs")
    train_metrics: List[Metrics] = Field([Metrics.iou], description="Which training metrics to use")
    val_metrics: List[Metrics] = Field([Metrics.f1, Metrics.iou, Metrics.precision, Metrics.recall],
                                       description="Which validation metrics to use")
    monitor: Metrics = Field(Metrics.iou, description="Metric to be monitored")
    patience: int = Field(25, description="Amount of epochs without improvement in the monitored metric")
    validate_every: int = Field(1, description="How many epochs between validation rounds")
    temperature: float = Field(2.0, description="Temperature for simulated annealing, >= 1")
    temp_epochs: int = Field(20, description="How many epochs before T goes back to 1")


class OptimizerConfig(InstantiableSettings):
    target: Optimizers = Field(Optimizers.adamw, description="Which optimizer to apply")
    lr: float = Field(1e-3, description="Global LR, still required to build optimizers")
    encoder_lr: float = Field(1e-3, description="Learning rate for the encoder branch")
    decoder_lr: float = Field(1e-3, description="Learning rate for the decoder branch")
    momentum: float = Field(0.9, description="Momentum for SGD")
    weight_decay: float = Field(1e-2, description="Weight decay for the optimizer")

    def instantiate(self, *args, **kwargs) -> Any:
        kwargs = dict(lr=self.lr, weight_decay=self.weight_decay, **kwargs)
        if self.target == Optimizers.sgd:
            kwargs.update(dict(momentum=self.momentum))
        return self.target(*args, **kwargs)


class SchedulerConfig(InstantiableSettings):
    target: Schedulers = Field(Schedulers.exp, description="Which scheduler to apply")

    def instantiate(self, *args, **kwargs) -> Any:
        return self.target(*args, **kwargs)


class LossConfig(InstantiableSettings):
    target: Losses = Field(Losses.bce, description="Which loss to apply")
    alpha: float = Field(0.6, description="Alpha param. for Tversky loss (0.5 for Dice)")
    beta: float = Field(0.4, description="Beta param. for Tversky loss (0.5 foor Dice)")
    gamma: float = Field(2.0, description="Gamma param. for focal loss (1.0 for standard CE)")
    reduction: str = Field("mean", description="How to reduce the loss")

    def instantiate(self, *args, **kwargs) -> Any:
        assert "ignore_index" in kwargs, "Ignore index required"
        # update current kwargs according to the loss
        if self.target == Losses.focal:
            kwargs.update(gamma=self.gamma)
        elif self.target == Losses.tversky:
            kwargs.update(alpha=self.alpha, beta=self.beta, gamma=self.gamma)
        return self.target(*args, **kwargs)


class DatasetConfig(EnvConfig):
    path: str = Field(required=True, description="Path to the dataset")
    in_channels: int = Field(3, description="How many input channels, including extras")
    include_dem: bool = Field(False, description="whether to include the DEM as extra input")
    class_weights: str = Field(None, description="Optional path to a class weight array (npy format)")
    mask_body_ratio: float = Field(None, description="Percentage of ones in the mask before discarding the tile")
    weighted_sampling: bool = Field(False, description="Whether to sample images based on flooded ratio")
    sample_smoothing: float = Field(0.8, description="Value between 0 and 1 to smooth out the weights (1 = None)")
    cache_hash: str = Field(None, description="Overwritten hash generated for cache from specific configuration")

    @validator("cache_hash")
    def post_load(cls, v, values, **kwargs):
        sha = hashlib.sha1(str(values).encode('utf-8'))
        hash = sha.hexdigest()
        return hash


class ModelConfig(EnvConfig):
    encoder: str = Field("resnet34", description="Which backbone to use (see timm library)")
    decoder: str = Field("pspnet", description="Which decoder to apply")
    pretrained: bool = Field(False, description="Whether to use a pretrained encoder or not")
    freeze: bool = Field(False, description="Freeze the feature extractor in incremental steps")
    multibranch: bool = Field(False, description="Includes an additional low-res output, right after the encoder")
    output_stride: int = Field(16, description="Output stride for ResNet-like models")
    act: ActivationLayers = Field(ActivationLayers.relu, description="Which activation layer to use")
    norm: NormLayers = Field(NormLayers.std, description="Which normalization layer to use")
    dropout2d: bool = Field(False, description="Whether to apply standard drop. or channel drop. to the last f.map")
    transforms: str = Field("", description="Automatically populated by the script for tracking purposes")

    @validator("norm")
    def post_load(cls, v, values, **kwargs):
        # override activation layer to identity for activated BNs (they already include it)
        if v in (NormLayers.iabn, NormLayers.iabn_sync):
            values["act"] = ActivationLayers.ident
        return v


class TrainConfig(BaseSettings):
    seed: int = Field(1337, description="Random seed for deterministic runs")
    image_size: int = Field(512, description="Size of the input images")
    trainer: TrainerConfig = TrainerConfig()
    # ML options
    data: DatasetConfig = DatasetConfig()
    model: ModelConfig = ModelConfig()
    loss: LossConfig = LossConfig()
    optimizer: OptimizerConfig = OptimizerConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    # logging options
    debug: bool = Field(False, description="Enables debug prints and logs")
    name: Optional[str] = Field(None, description="Identifier if the experiment, autogenerated if missing")
    output_folder: str = Field("outputs", description="Path to the folder to store outputs")
    num_samples: int = Field(8, description="How many sample batches to visualize, requires visualize=true")
    visualize: bool = Field(True, description="Turn on visualization on tensorboard")
    comment: str = Field("", description="Anything to describe your run, gets stored on tensorboard")
    version: str = Field("", description="Code version for tracking, obtained from git automatically")
