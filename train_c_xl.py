from warp_core import WarpCore
from warp_core.utils import DTO_REQUIRED
from dataclasses import dataclass
import torch
import torchvision
from torch import nn, optim
from transformers import AutoTokenizer, CLIPModel, CLIPVisionModelWithProjection
from warmup_scheduler import GradualWarmupScheduler

import sys
import os

from gdf import GDF, EpsilonTarget, CosineSchedule
from gdf import VPScaler, CosineTNoiseCond, DDPMSampler, P2LossWeight
from torchtools.transforms import SmartCrop

from modules.effnet import EfficientNetEncoder
from modules.stage_c import StageC
from modules.stage_c import ResBlock, AttnBlock, TimestepBlock, FeedForwardBlock
from modules.previewer import Previewer

from train_templates import DataCore, TrainingCore

from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.fsdp.wrap import ModuleWrapPolicy

class WurstCore(TrainingCore, DataCore, WarpCore):
    # DTOs ---------------------------------------
    @dataclass(frozen=True)
    class ConfigDTO(TrainingCore.ConfigDTO, DataCore.ConfigDTO, WarpCore.ConfigDTO):
        # TRAINING PARAMS
        lr: float = DTO_REQUIRED
        warmup_updates: int = DTO_REQUIRED

        # MODEL VERSION
        model_version: str = DTO_REQUIRED # 3.6B or 1B
        clip_image_model_name: str = 'openai/clip-vit-large-patch14'
        clip_text_model_name: str = 'laion/CLIP-ViT-bigG-14-laion2B-39B-b160k'

        # CHECKPOINT PATHS
        effnet_checkpoint_path: str = DTO_REQUIRED
        previewer_checkpoint_path: str = DTO_REQUIRED
        generator_checkpoint_path: str = None

    @dataclass(frozen=True)
    class ModelsDTO(TrainingCore.ModelsDTO, DataCore.ModelsDTO, WarpCore.ModelsDTO):
        effnet: nn.Module = DTO_REQUIRED
        previewer: nn.Module = DTO_REQUIRED


    @dataclass(frozen=True)
    class SchedulersDTO(WarpCore.SchedulersDTO):
        generator: any = None

    @dataclass(frozen=True)
    class ExtrasDTO(TrainingCore.ExtrasDTO, DataCore.ExtrasDTO, WarpCore.ExtrasDTO):
        gdf: GDF = DTO_REQUIRED
        sampling_configs: dict = DTO_REQUIRED
        effnet_preprocess: torchvision.transforms.Compose = DTO_REQUIRED

    # @dataclass() # not frozen, means that fields are mutable. Doesn't support DTO_REQUIRED
    # class InfoDTO(WarpCore.InfoDTO):
    #     ema_loss: float = None

    # @dataclass(frozen=True)
    # class OptimizersDTO(TrainingCore.OptimizersDTO, WarpCore.OptimizersDTO):
    #     generator : any = DTO_REQUIRED

    # --------------------------------------------
    info: TrainingCore.InfoDTO
    config: ConfigDTO

    # Extras: gdf, transforms and preprocessors --------------------------------
    def setup_extras_pre(self) -> ExtrasDTO:
        gdf = GDF(
            schedule = CosineSchedule(clamp_range=[0.0001, 0.9999]),
            input_scaler = VPScaler(), target = EpsilonTarget(),
            noise_cond = CosineTNoiseCond(),
            loss_weight = P2LossWeight(),
        )
        sampling_configs = {"cfg": 5, "sampler": DDPMSampler(gdf), "shift": 1, "timesteps": 20}

        effnet_preprocess = torchvision.transforms.Compose([
            torchvision.transforms.Normalize(
                mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)
            )
        ])

        clip_preprocess = torchvision.transforms.Compose([
            torchvision.transforms.Resize(224, interpolation=torchvision.transforms.InterpolationMode.BICUBIC),
            torchvision.transforms.CenterCrop(224),
            torchvision.transforms.Normalize(
                mean=(0.48145466, 0.4578275, 0.40821073), std=(0.26862954, 0.26130258, 0.27577711)
            )
        ])

        transforms = torchvision.transforms.Compose([
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Resize(self.config.image_size, interpolation=torchvision.transforms.InterpolationMode.BILINEAR, antialias=True),
            SmartCrop(self.config.image_size, randomize_p=0.3, randomize_q=0.2)
        ])

        return self.ExtrasDTO(
            gdf=gdf,
            sampling_configs=sampling_configs,
            transforms=transforms,
            effnet_preprocess=effnet_preprocess,
            clip_preprocess=clip_preprocess
        )

    # Data --------------------------------
    def get_conditions(self, batch: dict, models: ModelsDTO, extras: ExtrasDTO, is_eval=False, is_unconditional=False, eval_image_embeds=False, return_fields=None):
        conditions = super().get_conditions(
            batch, models, extras, is_eval, is_unconditional,
            eval_image_embeds, return_fields=return_fields or ['clip_text', 'clip_text_pooled', 'clip_img']
        )
        return conditions

    # Models, Optimizers & Schedulers setup --------------------------------
    def setup_models(self, extras: ExtrasDTO) -> ModelsDTO:
        # EfficientNet encoder
        effnet = EfficientNetEncoder().to(self.device)
        effnet_checkpoint = torch.load(self.config.effnet_checkpoint_path, map_location=self.device)
        effnet.load_state_dict(effnet_checkpoint if 'state_dict' not in effnet_checkpoint else effnet_checkpoint['state_dict'])
        effnet.eval().requires_grad_(False)
        del effnet_checkpoint

        # Previewer
        previewer = Previewer().to(self.device)
        previewer_checkpoint = torch.load(self.config.previewer_checkpoint_path, map_location=self.device)
        previewer.load_state_dict(previewer_checkpoint if 'state_dict' not in previewer_checkpoint else previewer_checkpoint['state_dict'])
        previewer.eval().requires_grad_(False)
        del previewer_checkpoint

        # Diffusion models
        if self.config.model_version == '3.6B':
            generator = StageC(switch_level=[True]).to(self.device)
            if self.config.ema_start_iters is not None:
                generator_ema = StageC(switch_level=[True]).to(self.device)
            else:
                generator_ema = None
        elif self.config.model_version == '1B':
            generator = StageC(c_cond=1536, c_hidden=[1536, 1536], nhead=[24, 24], blocks=[[4, 12], [12, 4]], switch_level=[True]).to(self.device)
            if self.config.ema_start_iters is not None:
                generator_ema = StageC(c_cond=1536, c_hidden=[1536, 1536], nhead=[24, 24], blocks=[[4, 12], [12, 4]], switch_level=[True]).to(self.device)
            else:
                generator_ema = None
        else:
            raise ValueError(f"Unknown model version {self.config.model_version}")

        if self.config.generator_checkpoint_path is not None:
            generator.load_state_dict(torch.load(self.config.generator_checkpoint_path, map_location=self.device))
        generator = self.load_model(generator, 'generator')

        if generator_ema is not None:
            generator_ema.load_state_dict(generator.state_dict())
            generator_ema = self.load_model(generator_ema, 'generator_ema')
            generator_ema.eval().requires_grad_(False)

        if self.config.use_fsdp:
            fsdp_auto_wrap_policy = ModuleWrapPolicy([ResBlock, AttnBlock, TimestepBlock, FeedForwardBlock])
            generator = FSDP(generator, **self.fsdp_defaults, auto_wrap_policy=fsdp_auto_wrap_policy, device_id=self.device)
            if generator_ema is not None:
                generator_ema = FSDP(generator_ema, **self.fsdp_defaults, auto_wrap_policy=fsdp_auto_wrap_policy, device_id=self.device)

        # CLIP encoders
        clip_tokenizer = AutoTokenizer.from_pretrained(self.config.clip_text_model_name)
        clip_model = CLIPModel.from_pretrained(self.config.clip_text_model_name)
        clip_text_model = clip_model.text_model.to(self.device).eval().requires_grad_(False)
        clip_text_model_proj = clip_model.text_projection.to(self.device).eval().requires_grad_(False)
        clip_image_model = CLIPVisionModelWithProjection.from_pretrained(self.config.clip_image_model_name).to(self.device).eval().requires_grad_(False)
        del clip_model

        return self.ModelsDTO(
            effnet=effnet, previewer=previewer,
            generator=generator, generator_ema=generator_ema,

            clip_tokenizer=clip_tokenizer, clip_text_model=clip_text_model,
            clip_text_model_proj=clip_text_model_proj, clip_image_model=clip_image_model
        )

    def setup_optimizers(self, extras: ExtrasDTO, models: ModelsDTO) -> TrainingCore.OptimizersDTO:
        optimizer = optim.AdamW(models.generator.parameters(), lr=self.config.lr) #, eps=1e-7, betas=(0.9, 0.95))
        optimizer = self.load_optimizer(optimizer, 'generator_optim', fsdp_model=models.generator if self.config.use_fsdp else None)
        return self.OptimizersDTO(generator=optimizer)

    def setup_schedulers(self, extras: ExtrasDTO, models: ModelsDTO, optimizers:TrainingCore.OptimizersDTO) -> SchedulersDTO:
        scheduler = GradualWarmupScheduler(optimizers.generator, multiplier=1, total_epoch=self.config.warmup_updates)
        scheduler.last_epoch = self.info.total_steps
        return self.SchedulersDTO(generator=scheduler)

    # Training loop --------------------------------
    def forward_pass(self, data: WarpCore.DataDTO, extras: ExtrasDTO, models: ModelsDTO):
        batch = next(data.iterator)

        with torch.no_grad():
            conditions = self.get_conditions(batch, models, extras)
            latents = self.encode_latents(batch, models, extras)
            noised, noise, target, logSNR, noise_cond, loss_weight = extras.gdf.diffuse(latents, shift=1, loss_shift=1)

        with torch.cuda.amp.autocast(dtype=torch.bfloat16):
            pred = models.generator(noised, noise_cond, **conditions)
            loss = nn.functional.mse_loss(pred, target, reduction='none').mean(dim=[1, 2, 3])
            loss_adjusted = (loss * loss_weight).mean() / self.config.grad_accum_steps

        return loss, loss_adjusted

    def backward_pass(self, update, loss, loss_adjusted, models: ModelsDTO, optimizers: TrainingCore.OptimizersDTO, schedulers: SchedulersDTO):
        if update:
            loss_adjusted.backward()
            grad_norm = nn.utils.clip_grad_norm_(models.generator.parameters(), 1.0)
            optimizers_dict = optimizers.to_dict()
            for k in optimizers_dict:
                optimizers_dict[k].step()
            schedulers_dict = schedulers.to_dict()
            for k in schedulers_dict:
                schedulers_dict[k].step()
            for k in optimizers_dict:
                optimizers_dict[k].zero_grad(set_to_none=True)
            self.info.total_steps += 1
        else:
            with models.generator.no_sync():
                loss_adjusted.backward()

        return grad_norm

    def models_to_save(self):
        return ['generator', 'generator_ema']

    # LATENT ENCODING & PROCESSING ----------
    def encode_latents(self, batch: dict, models: ModelsDTO, extras: ExtrasDTO) -> torch.Tensor:
        images = batch['images'].to(self.device)
        return models.effnet(extras.effnet_preprocess(images))

    def decode_latents(self, latents: torch.Tensor, batch: dict, models: ModelsDTO, extras: ExtrasDTO) -> torch.Tensor:
        return models.previewer(latents)

if __name__ == '__main__':
    print("Launching Script")
    warpcore = WurstCore(
        config_file_path=sys.argv[1] if len(sys.argv) > 1 else None,
        device=torch.device(int(os.environ.get("SLURM_LOCALID")))
    )
    # warp_core.fsdp_defaults['sharding_strategy'] = ShardingStrategy.NO_SHARD

    # RUN TRAINING
    warpcore()