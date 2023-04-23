import os
import json
import time
import datetime
import pytz
import socket
import threading
import traceback
import gradio as gr

from huggingface_hub import try_to_load_from_cache, snapshot_download

from ...config import Config
from ...globals import Global
from ...models import clear_cache, unload_models
from ...utils.prompter import Prompter
from ..trainer_callback import (
    UiTrainerCallback, reset_training_status,
    update_training_states, set_train_output
)

from .data_processing import get_data_from_input


def do_train(
    # Dataset
    template,
    load_dataset_from,
    dataset_from_data_dir,
    dataset_text,
    dataset_text_format,
    dataset_plain_text_input_variables_separator,
    dataset_plain_text_input_and_output_separator,
    dataset_plain_text_data_separator,
    # Training Options
    max_seq_length,
    evaluate_data_count,
    micro_batch_size,
    gradient_accumulation_steps,
    epochs,
    learning_rate,
    train_on_inputs,
    lora_r,
    lora_alpha,
    lora_dropout,
    lora_target_modules,
    lora_modules_to_save,
    load_in_8bit,
    fp16,
    bf16,
    gradient_checkpointing,
    save_steps,
    save_total_limit,
    logging_steps,
    additional_training_arguments,
    additional_lora_config,
    model_name,
    continue_from_model,
    continue_from_checkpoint,
    progress=gr.Progress(track_tqdm=False),
):
    if Global.is_training:
        return render_training_status()

    reset_training_status()
    Global.is_train_starting = True

    try:
        base_model_name = Global.base_model_name
        tokenizer_name = Global.tokenizer_name or Global.base_model_name

        resume_from_checkpoint_param = None
        if continue_from_model == "-" or continue_from_model == "None":
            continue_from_model = None
        if continue_from_checkpoint == "-" or continue_from_checkpoint == "None":
            continue_from_checkpoint = None
        if continue_from_model:
            resume_from_model_path = os.path.join(
                Config.data_dir, "lora_models", continue_from_model)
            resume_from_checkpoint_param = resume_from_model_path
            if continue_from_checkpoint:
                resume_from_checkpoint_param = os.path.join(
                    resume_from_checkpoint_param, continue_from_checkpoint)
                will_be_resume_from_checkpoint_file = os.path.join(
                    resume_from_checkpoint_param, "pytorch_model.bin")
                if not os.path.exists(will_be_resume_from_checkpoint_file):
                    raise ValueError(
                        f"Unable to resume from checkpoint {continue_from_model}/{continue_from_checkpoint}. Resuming is only possible from checkpoints stored locally in the data directory. Please ensure that the file '{will_be_resume_from_checkpoint_file}' exists.")
            else:
                will_be_resume_from_checkpoint_file = os.path.join(
                    resume_from_checkpoint_param, "adapter_model.bin")
                if not os.path.exists(will_be_resume_from_checkpoint_file):
                    # Try to get model in Hugging Face cache
                    resume_from_checkpoint_param = None
                    possible_hf_model_name = None
                    possible_model_info_file = os.path.join(
                        resume_from_model_path, "info.json")
                    if "/" in continue_from_model:
                        possible_hf_model_name = continue_from_model
                    elif os.path.exists(possible_model_info_file):
                        with open(possible_model_info_file, "r") as file:
                            model_info = json.load(file)
                            possible_hf_model_name = model_info.get(
                                "hf_model_name")
                    if possible_hf_model_name:
                        possible_hf_model_cached_path = try_to_load_from_cache(
                            possible_hf_model_name, 'adapter_model.bin')
                        if not possible_hf_model_cached_path:
                            snapshot_download(possible_hf_model_name)
                            possible_hf_model_cached_path = try_to_load_from_cache(
                                possible_hf_model_name, 'adapter_model.bin')
                        if possible_hf_model_cached_path:
                            resume_from_checkpoint_param = os.path.dirname(
                                possible_hf_model_cached_path)

                    if not resume_from_checkpoint_param:
                        raise ValueError(
                            f"Unable to continue from model {continue_from_model}. Continuation is only possible from models stored locally in the data directory. Please ensure that the file '{will_be_resume_from_checkpoint_file}' exists.")

        output_dir = os.path.join(Config.data_dir, "lora_models", model_name)
        if os.path.exists(output_dir):
            if (not os.path.isdir(output_dir)) or os.path.exists(os.path.join(output_dir, 'adapter_config.json')):
                raise ValueError(
                    f"The output directory already exists and is not empty. ({output_dir})")

        wandb_group = template
        wandb_tags = [f"template:{template}"]
        if load_dataset_from == "Data Dir" and dataset_from_data_dir:
            wandb_group += f"/{dataset_from_data_dir}"
            wandb_tags.append(f"dataset:{dataset_from_data_dir}")

        finetune_args = {
            'base_model': base_model_name,
            'tokenizer': tokenizer_name,
            'output_dir': output_dir,
            'micro_batch_size': micro_batch_size,
            'gradient_accumulation_steps': gradient_accumulation_steps,
            'num_train_epochs': epochs,
            'learning_rate': learning_rate,
            'cutoff_len': max_seq_length,
            'val_set_size': evaluate_data_count,
            'lora_r': lora_r,
            'lora_alpha': lora_alpha,
            'lora_dropout': lora_dropout,
            'lora_target_modules': lora_target_modules,
            'lora_modules_to_save': lora_modules_to_save,
            'train_on_inputs': train_on_inputs,
            'load_in_8bit': load_in_8bit,
            'fp16': fp16,
            'bf16': bf16,
            'gradient_checkpointing': gradient_checkpointing,
            'group_by_length': False,
            'resume_from_checkpoint': resume_from_checkpoint_param,
            'save_steps': save_steps,
            'save_total_limit': save_total_limit,
            'logging_steps': logging_steps,
            'additional_training_arguments': additional_training_arguments,
            'additional_lora_config': additional_lora_config,
            'wandb_api_key': Config.wandb_api_key,
            'wandb_project': Config.default_wandb_project if Config.enable_wandb else None,
            'wandb_group': wandb_group,
            'wandb_run_name': model_name,
            'wandb_tags': wandb_tags
        }

        prompter = Prompter(template)
        data = get_data_from_input(
            load_dataset_from=load_dataset_from,
            dataset_text=dataset_text,
            dataset_text_format=dataset_text_format,
            dataset_plain_text_input_variables_separator=dataset_plain_text_input_variables_separator,
            dataset_plain_text_input_and_output_separator=dataset_plain_text_input_and_output_separator,
            dataset_plain_text_data_separator=dataset_plain_text_data_separator,
            dataset_from_data_dir=dataset_from_data_dir,
            prompter=prompter
        )

        def training():
            Global.is_training = True

            try:
                # Need RAM for training
                unload_models()
                Global.new_base_model_that_is_ready_to_be_used = None
                Global.name_of_new_base_model_that_is_ready_to_be_used = None
                clear_cache()

                train_data = prompter.get_train_data_from_dataset(data)

                if Config.ui_dev_mode:
                    message = "Currently in UI dev mode, not doing the actual training."
                    message += f"\n\nArgs: {json.dumps(finetune_args, indent=2)}"
                    message += f"\n\nTrain data (first 5):\n{json.dumps(train_data[:5], indent=2)}"

                    print(message)

                    total_steps = 300
                    for i in range(300):
                        if (Global.should_stop_training):
                            break

                        current_step = i + 1
                        total_epochs = 3
                        current_epoch = i / 100
                        log_history = []

                        if (i > 20):
                            loss = 3 + (i - 0) * (0.5 - 3) / (300 - 0)
                            log_history = [{'loss': loss}]

                        update_training_states(
                            total_steps=total_steps,
                            current_step=current_step,
                            total_epochs=total_epochs,
                            current_epoch=current_epoch,
                            log_history=log_history
                        )
                        time.sleep(0.1)

                    result_message = set_train_output(message)
                    print(result_message)
                    time.sleep(1)
                    Global.is_training = False
                    return

                training_callbacks = [UiTrainerCallback]

                if not os.path.exists(output_dir):
                    os.makedirs(output_dir)

                with open(os.path.join(output_dir, "info.json"), 'w') as info_json_file:
                    dataset_name = "N/A (from text input)"
                    if load_dataset_from == "Data Dir":
                        dataset_name = dataset_from_data_dir

                    info = {
                        'base_model': base_model_name,
                        'prompt_template': template,
                        'dataset_name': dataset_name,
                        'dataset_rows': len(train_data),
                        'trained_on_machine': socket.gethostname(),
                        'timestamp': time.time(),
                    }
                    if continue_from_model:
                        info['continued_from_model'] = continue_from_model
                        if continue_from_checkpoint:
                            info['continued_from_checkpoint'] = continue_from_checkpoint

                    if Global.version:
                        info['tuner_version'] = Global.version

                    json.dump(info, info_json_file, indent=2)

                train_output = Global.finetune_train_fn(
                    train_data=train_data,
                    callbacks=training_callbacks,
                    **finetune_args,
                )

                result_message = set_train_output(train_output)
                print(result_message + "\n" + str(train_output))

                clear_cache()

                Global.is_training = False

            except Exception as e:
                traceback.print_exc()
                Global.training_error_message = str(e)
            finally:
                Global.is_training = False

        training_thread = threading.Thread(target=training)
        training_thread.daemon = True
        training_thread.start()

    except Exception as e:
        Global.is_training = False
        traceback.print_exc()
        Global.training_error_message = str(e)
    finally:
        Global.is_train_starting = False

    return render_training_status()


def render_training_status():
    if not Global.is_training:
        if Global.is_train_starting:
            html_content = """
            <div class="progress-block">
              <div class="progress-level">
                <div class="progress-level-inner">
                  Starting...
                </div>
              </div>
            </div>
            """
            return (gr.HTML.update(value=html_content), gr.HTML.update(visible=True))

        if Global.training_error_message:
            html_content = f"""
            <div class="progress-block is_error">
              <div class="progress-level">
                <div class="error">
                  <div class="title">
                    ⚠ Something went wrong
                  </div>
                  <div class="error-message">{Global.training_error_message}</div>
                </div>
              </div>
            </div>
            """
            return (gr.HTML.update(value=html_content), gr.HTML.update(visible=False))

        if Global.train_output_str:
            end_message = "✅ Training completed"
            if Global.should_stop_training:
                end_message = "🛑 Train aborted"
            html_content = f"""
            <div class="progress-block">
              <div class="progress-level">
                <div class="output">
                  <div class="title">
                    {end_message}
                  </div>
                  <div class="message">{Global.train_output_str}</div>
                </div>
              </div>
            </div>
            """
            return (gr.HTML.update(value=html_content), gr.HTML.update(visible=False))

        if Global.training_status_text:
            html_content = f"""
            <div class="progress-block">
              <div class="status">{Global.training_status_text}</div>
            </div>
            """
            return (gr.HTML.update(value=html_content), gr.HTML.update(visible=False))

        html_content = """
        <div class="progress-block">
          <div class="empty-text">
            Training status will be shown here
          </div>
        </div>
        """
        return (gr.HTML.update(value=html_content), gr.HTML.update(visible=False))

    meta_info = []
    meta_info.append(
        f"{Global.training_current_step}/{Global.training_total_steps} steps")
    current_time = time.time()
    time_elapsed = current_time - Global.train_started_at
    time_remaining = -1
    if Global.training_eta:
        time_remaining = Global.training_eta - current_time
    if time_remaining >= 0:
        meta_info.append(
            f"{format_time(time_elapsed)}<{format_time(time_remaining)}")
        meta_info.append(f"ETA: {format_timestamp(Global.training_eta)}")
    else:
        meta_info.append(format_time(time_elapsed))

    html_content = f"""
    <div class="progress-block is_training">
      <div class="meta-text">{' | '.join(meta_info)}</div>
      <div class="progress-level">
        <div class="progress-level-inner">
          {Global.training_status_text} - {Global.training_progress * 100:.2f}%
        </div>
        <div class="progress-bar-wrap">
          <div class="progress-bar" style="width: {Global.training_progress * 100:.2f}%;">
          </div>
        </div>
      </div>
    </div>
    """
    return (gr.HTML.update(value=html_content), gr.HTML.update(visible=True))


def format_time(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours == 0:
        return "{:02d}:{:02d}".format(int(minutes), int(seconds))
    else:
        return "{:02d}:{:02d}:{:02d}".format(int(hours), int(minutes), int(seconds))


def format_timestamp(timestamp):
    dt_naive = datetime.datetime.utcfromtimestamp(timestamp)
    utc = pytz.UTC
    timezone = Config.timezone
    dt_aware = utc.localize(dt_naive).astimezone(timezone)
    now = datetime.datetime.now(timezone)
    delta = dt_aware.date() - now.date()
    if delta.days == 0:
        time_str = ""
    elif delta.days == 1:
        time_str = "tomorrow at "
    elif delta.days == -1:
        time_str = "yesterday at "
    else:
        time_str = dt_aware.strftime('%A, %B %d at ')
    time_str += dt_aware.strftime('%I:%M %p').lower()
    return time_str