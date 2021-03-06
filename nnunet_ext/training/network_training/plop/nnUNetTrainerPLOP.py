#########################################################################################################
#----------------------This class represents the nnUNet trainer for PLOP training.----------------------#
#########################################################################################################

# -- This implementation represents the method proposed in the paper https://arxiv.org/pdf/2011.11390.pdf -- #
# -- PODNet for further details: https://arxiv.org/pdf/2004.13513.pdf -- #

import copy, torch
from time import time
from tqdm import trange
from operator import attrgetter
from torch.cuda.amp import autocast
from nnunet_ext.paths import default_plans_identifier
from nnunet.utilities.nd_softmax import softmax_helper
from nnunet.utilities.to_torch import maybe_to_torch, to_cuda
from batchgenerators.utilities.file_and_folder_operations import *
from nnunet_ext.training.loss_functions.crossentropy import entropy
from nnunet_ext.training.loss_functions.deep_supervision import MultipleOutputLossPLOP as PLOPLoss
from nnunet_ext.training.network_training.multihead.nnUNetTrainerMultiHead import nnUNetTrainerMultiHead

# -- Define globally the Hyperparameters for this trainer along with their type -- #
HYPERPARAMS = {'pod_lambda': float, 'scales': int}

class nnUNetTrainerPLOP(nnUNetTrainerMultiHead):
    def __init__(self, split, task, plans_file, fold, output_folder=None, dataset_directory=None, batch_dice=True, stage=None,
                 unpack_data=True, deterministic=True, fp16=False, save_interval=5, already_trained_on=None, use_progress=True,
                 identifier=default_plans_identifier, extension='plop', pod_lambda=1e-2, scales=3, tasks_list_with_char=None,
                 mixed_precision=True, save_csv=True, del_log=False, use_vit=False, vit_type='base', version=1, split_gpu=False,
                 transfer_heads=True, use_param_split=False, ViT_task_specific_ln=False, do_LSA=False, do_SPT=False, network=None):
        r"""Constructor of PLOP trainer for 2D, 3D low resolution and 3D full resolution nnU-Nets.
        """
        # -- Initialize using parent class -- #
        super().__init__(split, task, plans_file, fold, output_folder, dataset_directory, batch_dice, stage, unpack_data, deterministic,
                         fp16, save_interval, already_trained_on, use_progress, identifier, extension, tasks_list_with_char,
                         mixed_precision, save_csv, del_log, use_vit, vit_type, version, split_gpu, transfer_heads, use_param_split,
                         ViT_task_specific_ln, do_LSA, do_SPT, network)
        
        # -- Set the lambda scales variable for the PLOP Loss calculation during training -- #
        self.pod_lambda = pod_lambda
        self.scales = scales

        # -- Add flags in trained on file for restoring to be able to ensure that seed can not be changed during training -- #
        if already_trained_on is not None:
            # -- If the current fold does not exists initialize it -- #
            if self.already_trained_on.get(str(self.fold), None) is None:
                # -- Add the PLOP temperature and checkpoint settings -- #
                self.already_trained_on[str(self.fold)]['used_scales'] = self.scales
                self.already_trained_on[str(self.fold)]['used_pod_lambda'] = self.pod_lambda
                self.already_trained_on[str(self.fold)]['used_batch_size'] = int(self.batch_size) if self.batch_size is not None else None
            else: # It exists, then check if everything is in it
                # -- Define a list of all expected keys that should be in the already_trained_on dict for the current fold -- #
                keys = ['used_pod_lambda', 'used_scales', 'used_batch_size']
                assert all(key in self.already_trained_on[str(self.fold)] for key in keys),\
                    "The provided already_trained_on dictionary does not contain all necessary elements"
        else:
            # -- Update settings in trained on file for restoring to be able to ensure that scales can not be changed during training -- #
            self.already_trained_on[str(self.fold)]['used_scales'] = self.scales
            self.already_trained_on[str(self.fold)]['used_pod_lambda'] = self.pod_lambda
            self.already_trained_on[str(self.fold)]['used_batch_size'] = int(self.batch_size) if self.batch_size is not None else None

        # -- Update self.init_tasks so the storing works properly -- #
        self.init_args = (split, task, plans_file, fold, output_folder, dataset_directory, batch_dice, stage, unpack_data,
                          deterministic, fp16, save_interval, already_trained_on, use_progress, identifier, extension,
                          pod_lambda, scales, tasks_list_with_char, mixed_precision, save_csv, del_log, use_vit, self.vit_type,
                          version, split_gpu, transfer_heads, ViT_task_specific_ln, do_LSA, do_SPT)

        # -- Define the place holders for our results from the previous model on the current data -- #
        self.old_interm_results = dict()

        # -- Define empty dict for the current intermediate results during training -- #
        self.interm_results = dict()

        # -- Define placeholders for the thresholds and max_entropy and a flag to indicate if the loss is switched or not -- #
        self.thresholds, self.max_entropy = None, dict()
        self.switched = False

    def initialize(self, training=True, force_load_plans=False, num_epochs=500, prev_trainer_path=None, call_for_eval=False):
        r"""Overwrite the initialize function so the correct Loss function for the PLOP method can be set.
        """
        # -- Perform initialization of parent class -- #
        super().initialize(training, force_load_plans, num_epochs, prev_trainer_path, call_for_eval)

        # -- Reset the batch size to something that should fit for every network, so something small but not too small. -- #
        # -- Otherwise the sizes for the convolutional outputs (ie. the batch dim) don't match and they have to -- #
        self.batch_size = 100
        self.already_trained_on[str(self.fold)]['used_batch_size'] = int(self.batch_size)

        # -- Create a backup loss, so we can switch between original and PLOP loss -- #
        self.loss_orig = copy.deepcopy(self.loss)

        # -- Choose the right loss function (PLOP) that will be used during training -- #
        # -- --> Look into the Loss function to see how the approach is implemented -- #
        # -- Update the network paramaters during each iteration -- #
        self.loss_plop = PLOPLoss(self.num_classes-1, # Remove the background class since it has been added during initialization
                                  self.pod_lambda,
                                  self.scales,
                                  self.ds_loss_weights)

    def reinitialize(self, task):
        r"""This function is used to reinitialize the Trainer when a new task is trained for the PLOP Trainer.
            The most important thing here is that it sets the intermediate results accordingly in the loss.
            This should only be called when a new task is used --> by that time the new loss applies..
        """
        # -- Execute the super function -- #
        if len(self.mh_network.heads) == 1 and task in self.mh_network.heads:
            super().reinitialize(task, True)
        else:
            super().reinitialize(task, False)

            # -- Print Loss update -- #
            self.print_to_log_file("I am using PLOP loss now")

    def extract_max_entropy_and_thresholds(self):
        r"""This function extracts the max entropy and self.thresholds that are necessary for the pseudo label loss.
            Call this everytime before a new training starts.
            It finds the median prediction score per class using the old model.
            Extracted from here: https://github.com/arthurdouillard/CVPR2021_PLOP/blob/main/train.py#L505
        """
        # -- Update the log -- #
        self.print_to_log_file("Extracting the max_entropy and thresholds for pseudo labeling..")
        start_time = time()
        
        device = 'cuda:1' if self.split_gpu and not self.use_vit else 'cuda:0'
        self.max_entropy = torch.log(torch.tensor(self.num_classes).float().to(device))
        nb_bins = 100
        histograms = torch.zeros(self.num_classes, nb_bins).long().to(device)
        # -- Set softmax_helper --> just in case it is sth different -- #
        self.network_old.inference_apply_nonlin = softmax_helper
        # -- Set network to eval -- #
        self.network_old.eval()
        with trange(self.num_batches_per_epoch) as tbar:
            for _ in tbar:
                tbar.set_description("Extracting thresholds")
                data_dict = next(self.tr_gen)
                images = data_dict['data']
                labels = data_dict['target']
                images = maybe_to_torch(images)
                labels = maybe_to_torch(labels)
                # -- Put data on GPU -- #
                if torch.cuda.is_available():
                    images = to_cuda(images, gpu_id = int(device.split(':')[-1]))
                    labels = to_cuda(labels, gpu_id = int(device.split(':')[-1]))
                if self.fp16:
                    with autocast():
                        outputs_old = self.network_old(images)
                else:
                    outputs_old = self.network_old(images)
                for idx, outs in enumerate(outputs_old):
                    mask_bg = labels == 0
                    probas = self.network_old.inference_apply_nonlin(outs)
                    _, pseudo_labels = probas.max(dim=1)
                    values_to_bins = entropy(probas)[mask_bg].view(-1) / self.max_entropy
                    x_coords = pseudo_labels[mask_bg].view(-1)
                    y_coords = torch.clamp((values_to_bins * nb_bins).long(), max=nb_bins - 1)
                    histograms.index_put_((x_coords, y_coords), torch.LongTensor([1]).expand_as(x_coords).to(histograms.device), accumulate=True)
                    self.thresholds[idx] = torch.zeros(self.num_classes, dtype=torch.float32).to(device)
                    for c in range(self.num_classes):
                        total = histograms[c].sum()
                        if total <= 0.:
                            continue
                        half = total / 2
                        running_sum = 0.
                        for lower_border in range(nb_bins):
                            lower_border = lower_border / nb_bins
                            bin_index = int(lower_border * nb_bins)
                            if half >= running_sum and half <= (running_sum + histograms[c, bin_index]):
                                break
                            running_sum += lower_border * nb_bins
                        median = lower_border + ((half - running_sum) / histograms[c, bin_index].sum()) * (1 / nb_bins)
                        self.thresholds[idx][c] = median
                    base_threshold = 0.001
                    for c in range(len(self.thresholds[idx])):
                        self.thresholds[idx][c] = max(self.thresholds[idx][c], base_threshold)

        # -- Put thresholds and max_entropy on device 0 for the loss calculation -- #
        if self.split_gpu and self.use_vit:
            self.max_entropy.cuda()
            for _, threshold in self.thresholds.items():
                threshold.cuda()

        # -- Update the log -- #
        self.print_to_log_file("Extraction took %.2f seconds" % (time() - start_time))

    def run_training(self, task, output_folder):
        r"""Overwrite super class to adapt for PLOP training method.
        """
        # -- Create a deepcopy of the previous, ie. currently set model if we do PLOP training -- #
        if task not in self.mh_network.heads:
            self.network_old = copy.deepcopy(self.network)
            # -- Save this network using checkpoint saving for restoring purposes -- #
            self.save_checkpoint(join(self.output_folder, "model_latest.model"), old_model=True, fname_old=join(self.output_folder, "model_old.model"))

            if self.split_gpu and not self.use_vit:
                self.network_old.cuda(1)    # Put on second GPU

            # -- Extract the self.thresholds and self.max_entropy values -- #
            self.extract_max_entropy_and_thresholds()
            
            # -- Register the hook here as well -- #
            self.register_forward_hooks(old=True)

        # -- In case of restoring this ensures everything will be set -- #
        if len(self.mh_network.heads) > 1 and (self.max_entropy is None or not self.thresholds):
            # -- If the max_entropy and thresholds do not exist, calculate them -- #
            self.extract_max_entropy_and_thresholds()

        # -- Run training using parent class -- #
        ret = super().run_training(task, output_folder)

        # -- Reset max_entropy and thresholds -- #
        self.max_entropy, self.thresholds = None, dict()

        # -- Return the result -- #
        return ret

    def run_iteration(self, data_generator, do_backprop=True, run_online_evaluation=False, detach=True, no_loss=False, pod=False):
        r"""This function needs to be changed for the PLOP method, since intermediate results will be used within
            the Loss function to compute the Loss as proposed in the paper.
        """
        # -- Ensure that the first task is trained as usual and the validation without the plop loss as well -- #
        if self.task in self.mh_network.heads and len(self.mh_network.heads) == 1 or run_online_evaluation: # The very first task
            # -- Use the original loss for this -- #
            self.loss = self.loss_orig
            self.switched = False
            # -- Run iteration as usual using parent class -- #
            loss = super().run_iteration(data_generator, do_backprop, run_online_evaluation, detach, no_loss)
            # -- NOTE: If this is called during _perform_validation, run_online_evaluation is true --> Does not matter -- #
            # --       which loss is used, since we only calculate Dice and IoU and do not keep track of the loss -- #
        else:   # --> More than one head, ie. trained on more than one task  --> use PLOP
            if not self.switched:
                # -- Switch to plop loss -- #
                self.loss = self.loss_plop
                # -- We are at a further sequence of training, so we train using the PLOP method -- #
                self.register_forward_hooks()   # --> Just in case it is not already done, ie. after first task training!
                self.switched = True
            #------------------------------------------ Partially copied from original implementation ------------------------------------------#
            # -- Extract data -- #
            data_dict = next(data_generator)
            data = data_dict['data']
            target = data_dict['target']
            # -- Transform data to torch if necessary -- #
            data = maybe_to_torch(data)
            target = maybe_to_torch(target)
            # -- Put data on GPU -- #
            if torch.cuda.is_available():
                data = to_cuda(data)
                target = to_cuda(target)

            self.optimizer.zero_grad()

            if self.fp16:
                with autocast():
                    output = self.network(data) # --> self.interm_results is filled with intermediate result now!
                    # -- Extract the old results using the old network -- #
                    if self.split_gpu and not self.use_vit:
                        data = to_cuda(data, gpu_id=1)
                    output_o = self.network_old(data) # --> self.old_interm_results is filled with intermediate result now!
                    (x.detach for x in output_o)
                    del data
                    # -- Put old_interm_results on same GPU as interm_results -- #
                    if self.split_gpu and not self.use_vit:
                        for key in self.old_interm_results:
                            self.old_interm_results[key] = to_cuda(self.old_interm_results[key], gpu_id=self.interm_results[key].device)

                    # -- Update the loss with the data -- #
                    if pod:
                        self.loss.update_plop_params(self.old_interm_results, self.interm_results)
                        if not no_loss:
                            loss = self.loss(output, target)
                    else:
                        self.loss.update_plop_params(self.old_interm_results, self.interm_results, self.thresholds, self.max_entropy)
                        if not no_loss:
                            loss = self.loss(output, output_o, target)

                if do_backprop:
                    self.amp_grad_scaler.scale(loss).backward()
                    self.amp_grad_scaler.unscale_(self.optimizer)
                    torch.nn.utils.clip_grad_norm_(self.network.parameters(), 12)
                    self.amp_grad_scaler.step(self.optimizer)
                    self.amp_grad_scaler.update()
            else:
                output = self.network(data)
                if self.split_gpu and not self.use_vit:
                    data = to_cuda(data, gpu_id=1)
                output_o = self.network_old(data)
                (x.detach for x in output_o)
                del data
                # -- Put old_interm_results on same GPU as interm_results -- #
                if self.split_gpu and not self.use_vit:
                    for key in self.old_interm_results:
                        self.old_interm_results[key] = to_cuda(self.old_interm_results[key], gpu_id=self.interm_results[key].device)
                # -- Update the loss with the data -- #
                if pod:
                    self.loss.update_plop_params(self.old_interm_results, self.interm_results)
                    if not no_loss:
                        loss = self.loss(output, target)
                else:
                    self.loss.update_plop_params(self.old_interm_results, self.interm_results, self.thresholds, self.max_entropy)
                    if not no_loss:
                        loss = self.loss(output, output_o, target)
                
                if do_backprop:
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(self.network.parameters(), 12)
                    self.optimizer.step()

            if run_online_evaluation:
                self.run_online_evaluation(output, target)

            del target
            #------------------------------------------ Partially copied from original implementation ------------------------------------------#
        
            # -- Update the Multi Head Network after one iteration only if backprop is performed (during training) -- #
            if do_backprop:
                self.mh_network.update_after_iteration()

            # -- Detach the loss -- #
            if detach:
                loss = loss.detach().cpu().numpy()

            # -- Empty the dicts -- #
            self.old_interm_results = dict()
            self.interm_results = dict()

        # -- Return the loss -- #
        if not no_loss:
            return loss

    def register_forward_hooks(self, old=False):
        r"""This function sets the forward hooks for every convolutional layer in the network.
            The old parameter indicates that the old network should be used to register the hooks.
        """
        # -- Set the correct network to use -- #
        use_network = self.network_old if old else self.network

        # -- Extract all module names that are of any convolutional type -- #
        module_names = [name for name, module in use_network.named_modules() if 'conv.Conv' in str(type(module))]

        # -- Register hooks -- #
        for mod in module_names:
            attrgetter(mod)(use_network).register_forward_hook(self._get_activation(mod, old))

    def _get_activation(self, name, old=False):
        r"""This function returns the hook given a specific (module) name that needs to be
            registered to the module before calling it with actual data.
        """
        def hook(model, input, output):
            if old:
                self.old_interm_results[name]  = output.detach()     # Store the output in the dict at corresponding name
            else:
                self.interm_results[name] = output.detach()     # Store the output in the dict at corresponding name
        return hook
