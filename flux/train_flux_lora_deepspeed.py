import argparse
import logging
import math
import os
import re
import random
import shutil
from contextlib import nullcontext
from pathlib import Path
from safetensors.torch import save_file

import accelerate
import datasets
import numpy as np
import torch
import torch.nn.functional as F
import torch.utils.checkpoint
import transformers
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.state import AcceleratorState
from accelerate.utils import ProjectConfiguration, set_seed
from huggingface_hub import create_repo, upload_folder
from packaging import version
from tqdm.auto import tqdm
from transformers import CLIPTextModel, CLIPTokenizer
from transformers.utils import ContextManagers
from omegaconf import OmegaConf
from copy import deepcopy
import diffusers
from diffusers import AutoencoderKL, DDPMScheduler, StableDiffusionPipeline
from diffusers.optimization import get_scheduler
from diffusers.training_utils import EMAModel, compute_dream_and_update_latents, compute_snr
from diffusers.utils import check_min_version, deprecate, is_wandb_available, make_image_grid
from diffusers.utils.hub_utils import load_or_create_model_card, populate_model_card
from diffusers.utils.import_utils import is_xformers_available
from diffusers.utils.torch_utils import is_compiled_module
from einops import rearrange
from src.flux.sampling import denoise, get_noise, get_schedule, prepare, unpack
from src.flux.util import (configs, load_ae, load_clip,
                       load_flow_model2, load_t5)
from src.flux.modules.layers import DoubleStreamBlockLoraProcessor, SingleStreamBlockLoraProcessor
from src.flux.xflux_pipeline import XFluxSampler

from image_datasets.dataset import loader
if is_wandb_available():
    import wandb
logger = get_logger(__name__, log_level="INFO")

def get_models(name: str, device, offload: bool, is_schnell: bool):
    t5 = load_t5(device, max_length=256 if is_schnell else 512)
    clip = load_clip(device)
    clip.requires_grad_(False)
    model = load_flow_model2(name, device="cpu")
    vae = load_ae(name, device="cpu" if offload else device)
    return model, vae, t5, clip

def parse_args():
    parser = argparse.ArgumentParser(description="Simple example of a training script.")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        required=True,
        help="path to config",
    )
    args = parser.parse_args()


    return args.config


def main():
    args = OmegaConf.load(parse_args())
    is_schnell = args.model_name == "flux-schnell"
    logging_dir = os.path.join(args.output_dir, args.logging_dir)

    accelerator_project_config = ProjectConfiguration(project_dir=args.output_dir, logging_dir=logging_dir)

    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=args.mixed_precision,
        log_with=args.report_to,
        project_config=accelerator_project_config,
    )

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_warning()
        diffusers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()
        diffusers.utils.logging.set_verbosity_error()


    if accelerator.is_main_process:
        if args.output_dir is not None:
            os.makedirs(args.output_dir, exist_ok=True)

    # Load model and tokenizer
    dit, vae, t5, clip = get_models(name=args.model_name, device=accelerator.device, offload=False, is_schnell=is_schnell)
    lora_attn_procs = {}

    if args.double_blocks is None:
        double_blocks_idx = list(range(19))
    else:
        double_blocks_idx = [int(idx) for idx in args.double_blocks.split(",")]

    if args.single_blocks is None:
        single_blocks_idx = list(range(38))
    elif args.single_blocks is not None:
        single_blocks_idx = [int(idx) for idx in args.single_blocks.split(",")]

    # 单流双流注意力模块均需要增加LoRA方法
    for name, attn_processor in dit.attn_processors.items():
        match = re.search(r'\.(\d+)\.', name)  # 在name中搜索形如.数字.的模式，并尝试匹配中间的数字，这个变量表示处理器所在的层索引。
        if match:
            layer_index = int(match.group(1))

        if name.startswith("double_blocks") and layer_index in double_blocks_idx:
            print("setting LoRA Processor for", name)
            lora_attn_procs[name] = DoubleStreamBlockLoraProcessor(
              dim=3072, rank=args.rank
            )  # 添加LoRA处理器到对应的模块
        elif name.startswith("single_blocks") and layer_index in single_blocks_idx:
            print("setting LoRA Processor for", name)
            lora_attn_procs[name] = SingleStreamBlockLoraProcessor(
              dim=3072, rank=args.rank
            )
        else:
            lora_attn_procs[name] = attn_processor

    dit.set_attn_processor(lora_attn_procs)

    vae.requires_grad_(False)
    t5.requires_grad_(False)
    clip.requires_grad_(False)
    dit = dit.to(torch.float32)
    dit.train()
    optimizer_cls = torch.optim.AdamW
    # freeze all params but LoRA
    for n, param in dit.named_parameters():
        if '_lora' not in n:
            param.requires_grad = False
        else:
            print(n)
    print(sum([p.numel() for p in dit.parameters() if p.requires_grad]) / 1000000, 'parameters')
    optimizer = optimizer_cls(
        [p for p in dit.parameters() if p.requires_grad],
        lr=args.learning_rate,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.adam_weight_decay,
        eps=args.adam_epsilon,
    )
    print("data_config:", args.data_config)
    train_dataloader = loader(**args.data_config)
    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    # 通过 gradient_accumulation_steps 模拟大批量训练
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps * accelerator.num_processes,
        num_training_steps=args.max_train_steps * accelerator.num_processes,
    )
    global_step = 0
    first_epoch = 0
    # 使用 HuggingFace Accelerate 库封装模型、优化器、数据加载器和调度器
    dit, optimizer, _, lr_scheduler = accelerator.prepare(
        dit, optimizer, deepcopy(train_dataloader), lr_scheduler
    )

    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
        args.mixed_precision = accelerator.mixed_precision
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16
        args.mixed_precision = accelerator.mixed_precision

    # 训练步数与Epoch数动态调整
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    # 仅在主进程初始化实验跟踪器（如Weights & Biases、TensorBoard）。
    if accelerator.is_main_process:
        accelerator.init_trackers(args.tracker_project_name, {"test": None})

    timesteps = get_schedule(
                999,
                (1024 // 8) * (1024 // 8) // 4,
                shift=True,
            )
    total_batch_size = args.train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint != "latest":
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = os.listdir(args.output_dir)
            dirs = [d for d in dirs if d.startswith("checkpoint")]
            dirs = sorted(dirs, key=lambda x: int(x.split("-")[1]))
            path = dirs[-1] if len(dirs) > 0 else None

        if path is None:
            accelerator.print(
                f"Checkpoint '{args.resume_from_checkpoint}' does not exist. Starting a new training run."
            )
            args.resume_from_checkpoint = None
            initial_global_step = 0
        else:
            accelerator.print(f"Resuming from checkpoint {path}")
            accelerator.load_state(os.path.join(args.output_dir, path))
            global_step = int(path.split("-")[1])

            initial_global_step = global_step
            first_epoch = global_step // num_update_steps_per_epoch

    else:
        initial_global_step = 0
    progress_bar = tqdm(
        range(0, args.max_train_steps),
        initial=initial_global_step,
        desc="Steps",
        disable=not accelerator.is_local_main_process,
    )

    for epoch in range(first_epoch, args.num_train_epochs):
        train_loss = 0.0
        for step, batch in enumerate(train_dataloader):
            # 启用梯度累积，将多个小批次的梯度累加后再执行参数更新。
            with accelerator.accumulate(dit):
                img, prompts = batch
                with torch.no_grad():
                    # 图像编码:使用VAE将原始图像编码为潜在表示 x_1，降低空间维度（如从256x256→64x64)
                    x_1 = vae.encode(img.to(accelerator.device).to(torch.float32))
                    # 文本编码:通过 prepare 函数生成文本嵌入(包含T5和CLIP的双编码器)
                    inp = prepare(t5=t5, clip=clip, img=x_1, prompt=prompts)
                    # 张量重塑:将空间维度展平为序列维度（如64x64→4096）,适配Transformer结构
                    x_1 = rearrange(x_1, "b c (h ph) (w pw) -> b (h w) (c ph pw)", ph=2, pw=2)

                bs = img.shape[0]
                # 时间步采样
                t = torch.tensor([timesteps[random.randint(0, 999)]]).to(accelerator.device)
                x_0 = torch.randn_like(x_1).to(accelerator.device)
                # 噪声混合,定义从噪声分布到目标分布的路径
                x_t = (1 - t) * x_1 + t * x_0
                bsz = x_1.shape[0]
                guidance_vec = torch.full((x_t.shape[0],), 1, device=x_t.device, dtype=x_t.dtype)

                # Predict the noise residual and compute loss
                model_pred = dit(img=x_t.to(weight_dtype),
                                img_ids=inp['img_ids'].to(weight_dtype),
                                txt=inp['txt'].to(weight_dtype),
                                txt_ids=inp['txt_ids'].to(weight_dtype),
                                y=inp['vec'].to(weight_dtype),
                                timesteps=t.to(weight_dtype),
                                guidance=guidance_vec.to(weight_dtype),)
                # 计算损失函数, 其中model_pred是模型预测的结果，x_0是真实的噪声，x_1是真实的图像
                loss = F.mse_loss(model_pred.float(), (x_0 - x_1).float(), reduction="mean")

                # Gather the losses across all processes for logging (if we use distributed training).
                # 收集所有进程的损失值并计算全局均值。
                avg_loss = accelerator.gather(loss.repeat(args.train_batch_size)).mean()
                train_loss += avg_loss.item() / args.gradient_accumulation_steps

                # Backpropagate
                # 自动处理混合精度下的梯度缩放。
                accelerator.backward(loss)
                # 多GPU训练时同步梯度（如使用DDP）。
                if accelerator.sync_gradients:
                    # 防止梯度爆炸，限制全局梯度范数。
                    accelerator.clip_grad_norm_(dit.parameters(), args.max_grad_norm)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                global_step += 1
                accelerator.log({"train_loss": train_loss}, step=global_step)
                train_loss = 0.0

                if not args.disable_sampling and global_step % args.sample_every == 0:
                    if accelerator.is_main_process:
                        print(f"Sampling images for step {global_step}...")
                        sampler = XFluxSampler(clip=clip, t5=t5, ae=vae, model=dit, device=accelerator.device)
                        images = []
                        for i, prompt in enumerate(args.sample_prompts):
                            result = sampler(prompt=prompt,
                                             width=args.sample_width,
                                             height=args.sample_height,
                                             num_steps=args.sample_steps
                                             )
                            images.append(wandb.Image(result))
                            print(f"Result for prompt #{i} is generated")
                            # result.save(f"{global_step}_prompt_{i}_res.png")
                        wandb.log({f"Results, step {global_step}": images})

                if global_step % args.checkpointing_steps == 0:
                    if accelerator.is_main_process:
                        # _before_ saving state, check if this save would set us over the `checkpoints_total_limit`
                        if args.checkpoints_total_limit is not None:
                            checkpoints = os.listdir(args.output_dir)
                            checkpoints = [d for d in checkpoints if d.startswith("checkpoint")]
                            checkpoints = sorted(checkpoints, key=lambda x: int(x.split("-")[1]))

                            # before we save the new checkpoint, we need to have at _most_ `checkpoints_total_limit - 1` checkpoints
                            if len(checkpoints) >= args.checkpoints_total_limit:
                                num_to_remove = len(checkpoints) - args.checkpoints_total_limit + 1
                                removing_checkpoints = checkpoints[0:num_to_remove]

                                logger.info(
                                    f"{len(checkpoints)} checkpoints already exist, removing {len(removing_checkpoints)} checkpoints"
                                )
                                logger.info(f"removing checkpoints: {', '.join(removing_checkpoints)}")

                                for removing_checkpoint in removing_checkpoints:
                                    removing_checkpoint = os.path.join(args.output_dir, removing_checkpoint)
                                    shutil.rmtree(removing_checkpoint)

                    save_path = os.path.join(args.output_dir, f"checkpoint-{global_step}")

                    accelerator.save_state(save_path)
                    unwrapped_model_state = accelerator.unwrap_model(dit).state_dict()

                    # save checkpoint in safetensors format
                    lora_state_dict = {k:unwrapped_model_state[k] for k in unwrapped_model_state.keys() if '_lora' in k}
                    save_file(
                        lora_state_dict,
                        os.path.join(save_path, "lora.safetensors")
                    )

                    logger.info(f"Saved state to {save_path}")

            logs = {"step_loss": loss.detach().item(), "lr": lr_scheduler.get_last_lr()[0]}
            progress_bar.set_postfix(**logs)

            if global_step >= args.max_train_steps:
                break

    accelerator.wait_for_everyone()
    accelerator.end_training()


if __name__ == "__main__":
    main()
