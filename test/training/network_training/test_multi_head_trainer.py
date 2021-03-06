##############################################################################################
# -- Test suite to test the Multi Head Trainer provided with this extension of the nnUNet -- #
##############################################################################################

import torch
import numpy as np
import os, sys, copy
from time import time
import time as time_mod
from nnunet.configuration import default_num_threads
from nnunet_ext.utilities.helpful_functions import *
from nnunet_ext.paths import nnUNet_raw_data as old_nnUNet_raw_data # Do this otherwise reassignment does not work
from nnunet_ext.run.default_configuration import get_default_configuration
from nnunet_ext.paths import nnUNet_cropped_data as old_nnUNet_cropped_data
from nnunet_ext.paths import my_output_identifier, default_plans_identifier
from nnunet_ext.scripts.delete_specified_task import main as delete_specified_task
from nnunet_ext.paths import preprocessing_output_dir as old_preprocessing_output_dir
from nnunet_ext.utilities.helpful_functions import refresh_mod_imports as refresh_imports
from nnunet_ext.paths import network_training_output_dir as old_network_training_output_dir
from nnunet_ext.experiment_planning.dataset_label_mapping import main as dataset_label_mapping
from nnunet_ext.paths import network_training_output_dir_base as old_network_training_output_dir_base
from nnunet.run.default_configuration import get_default_configuration as nn_get_default_configuration
from nnunet_ext.training.network_training.multihead.nnUNetTrainerMultiHead import nnUNetTrainerMultiHead # Own implemented class
from batchgenerators.utilities.file_and_folder_operations import maybe_mkdir_p, join, load_json, save_json

def equal_models(model_1, model_2):
    r"""This function is used to compare two PyTorch Modules and return if they are both identical based
        on their state_dicts().
        Based on: https://discuss.pytorch.org/t/check-if-models-have-same-weights/4351/5
    """
    # -- Set difference counter to 0 -- #
    models_differ = 0
    # -- Loop throught the state_dicts -- #
    for key_item_1, key_item_2 in zip(model_1.state_dict().items(), model_2.state_dict().items()):
        # -- When they are identical continue with next loop element -- #
        if torch.equal(key_item_1[1], key_item_2[1]):
            pass
        # -- If they are not equal -- #
        else:
            # -- Check if the keys are identical -- #
            if (key_item_1[0] == key_item_2[0]):
                models_differ += 1  # Then there is a true difference in the model
    # -- Return if the models are equal (True) or different (False)
    return models_differ == 0

# -- Define the Exception that is thrown if the 3d_lowres initialization fails because it can not be trained on 3d_lowres -- #
class SkipNetwork(Exception):
    pass

# -- Start testing --> This suite tests the Multi Head Trainer -- #
def test_multi_head_trainer(ext_map=None, args_f=None):
    r"""This function is used to test the Multi Head Trainer (or other trainers) which uses the Multi Head Network Module.
        The Multi Head Module is tested in a seperate test. The dataset_label_mapping function is also
        tested in a seperate test suite."""
    # ----------------------------------- #
    # ------ Prepare data for test ------ #
    # ----------------------------------- #
    # -- Get the current timestamp -- #
    start_time = time()

    # -- Define the log file and output folder where it will be stored-- #
    log_file = None # Create it in first call
    output_folder = old_network_training_output_dir_base  # Store the log file were training is performed

    # -- Modify the paths, so the planning/preprocessing/training is stored differently and can then be removed after testing -- #
    # -- Save the folder name that includes all data generated for this test --> this one will be removed after the test -- #
    base = join(os.path.dirname(os.path.realpath(old_nnUNet_raw_data)), "tmp_for_testing")  # --> Base path
    nnUNet_raw_data = join(base, "nnUNet_raw_data")
    folder_name = old_preprocessing_output_dir.split(os.sep)[-1]
    preprocessing_output_dir = join(base, folder_name)
    nnUNet_cropped_data = join(base, "nnUNet_cropped_data")
    folder_name = old_network_training_output_dir.split(os.sep)[-2]
    network_training_output_dir = join(base, folder_name, my_output_identifier)
    mapping_folder = join(base, 'mappings')
    del folder_name
    
    # --> All paths are set and we can start with data preprocessing -- #
    # -- Delete the directory if it already exists due to test from previous run that terminated in an error -- #
    if os.path.isdir(base):
        delete_dir_con(base)
        
    # -- But first create those directories -- #
    maybe_mkdir_p(nnUNet_raw_data)
    maybe_mkdir_p(preprocessing_output_dir)
    maybe_mkdir_p(nnUNet_cropped_data)
    maybe_mkdir_p(network_training_output_dir)
    maybe_mkdir_p(mapping_folder)

    # -- Create the Log file and store those folders in there as a note -- #
    log_file = print_to_log_file(log_file, output_folder, 'pytest_log', "Take raw data from: {}".format(os.path.dirname(os.path.realpath(old_nnUNet_raw_data))))
    log_file = print_to_log_file(log_file, output_folder, 'pytest_log', "Store raw data with new task names at: {}".format(nnUNet_raw_data))
    log_file = print_to_log_file(log_file, output_folder, 'pytest_log', "Store preprocessed data at: {}".format(preprocessing_output_dir))
    log_file = print_to_log_file(log_file, output_folder, 'pytest_log', "Store copped data at: {}".format(nnUNet_cropped_data))
    log_file = print_to_log_file(log_file, output_folder, 'pytest_log', "Store output from training at: {}".format(network_training_output_dir))
    log_file = print_to_log_file(log_file, output_folder, 'pytest_log', "The folder that will be deleted once the training is finished sucessfully: {}".format(base))

    # -- Preprocess and plan all the data that is necessary for this test suite -- #
    # -- We use Hippocampus, Heart and Prostate, whereas prostate also changes labels and is only done on first channel -- #
    # -- Generate the mapping files for those tasks, whereas the label mapping is not changed -- #
    log_file = print_to_log_file(log_file, output_folder, 'pytest_log', "Start creating the mapping files and storing them at: {}".format(mapping_folder))
    # -- Define the list of tasks to load the correct dataset.json file -- #
    tasks = ['Task04_Hippocampus', 'Task02_Heart']#, 'Task05_Prostate'
    # -- Loop through those tasks, load the dataset.json file and build a mapping file -- #
    for task in tasks:
        # -- Load the dataset file -- #
        dataset_file = load_json(join(os.path.dirname(os.path.realpath(old_nnUNet_raw_data)), task, 'dataset.json'))
        # -- Extract the mapping -- #
        mapping = dict()
        for label, description in dataset_file['labels'].items():
            # -- Do not change the label --> this is necessary for the function to work since we abuse it, -- #
            # -- normally it is used to change those labels but now we do not want that -- #
            mapping[str(description) + ' --> ' + str(label)] = int(label)
        # -- Save the mappings file with same name as task -- #
        save_json(mapping, join(mapping_folder, str(task)+'.json'))
        # -- Update the log file -- #
        log_file = print_to_log_file(log_file, output_folder, 'pytest_log',\
            "Created mapping file for task {}:\n{}".format(task, mapping))
        log_file = print_to_log_file(log_file, output_folder, 'pytest_log', \
            "This mapping file is stored at: {}".format(join(mapping_folder, str(task)+'.json')))
    
    # -- Preprocess data using the dataset_label_mapping class -- #
    tasks_in_path = [join(os.path.dirname(os.path.realpath(old_nnUNet_raw_data)), 'Task04_Hippocampus'),\
                     join(os.path.dirname(os.path.realpath(old_nnUNet_raw_data)), 'Task02_Heart')]
    tasks_out_ids = [-11, -22]  # Those tasks should not exist since they will be stored at the original paths, we need to move them
    mapping_files_path = [join(mapping_folder, 'Task04_Hippocampus.json'), join(mapping_folder, 'Task02_Heart.json')]
    channels = ['all']
    p = default_num_threads
    no_pp = False
    # -- Plan and preprocess those datasets
    dataset_label_mapping(False, tasks_in_path=tasks_in_path, tasks_out_ids=tasks_out_ids, mapping_files_path=mapping_files_path, name=None,
                          channels=channels, p=p, no_pp=no_pp)

    # -- Prepare to plan and preprocess the prostate dataset -- #
    # tasks_in_path = [join(os.path.dirname(os.path.realpath(old_nnUNet_raw_data)), 'Task05_Prostate')]
    # mapping_files_path = [join(mapping_folder, 'Task05_Prostate.json')]
    # -- Plan and preprocess the prostate dataset -- #
    # dataset_label_mapping(False, tasks_in_path=tasks_in_path, tasks_out_ids=[-33], mapping_files_path=mapping_files_path, name=None
    #                       channels=[0], p=p, no_pp=no_pp)
    
    # -- Update the log file for the last time -- #
    execution_time = time() - start_time
    log_file = print_to_log_file(log_file, output_folder, 'pytest_log', \
            "This planning and preprocessing took {:.2f} seconds ({}).\n".format(execution_time, time_mod.strftime('%H:%M:%S', time_mod.gmtime(execution_time))))

    # ------------------------------- #
    # ------ Setup for testing ------ #
    # ------------------------------- #
    # -- Define the fold to train on, the split, networks and task names for the experiments -- #
    fold = 0
    split = 'seg_outputs'
    networks = ['2d', '3d_lowres', '3d_fullres']
    tasks = ['Task-11_Hippocampus', 'Task-22_Heart']#, 'Task-33_Prostate']

    # -- Join all task names together with a '_' in between them -- #
    char_to_join_tasks = '_'
    tasks_list_with_char = (tasks, char_to_join_tasks)
    tasks_joined_name = join_texts_with_char(tasks, char_to_join_tasks)

    # -- Build mapping file for trainers to test if not transmitted -- #
    if ext_map is None:
        ext_map = {'multihead': nnUNetTrainerMultiHead}
    
    # -- Join the dictionaries into a dictionary with the corresponding class name if nothing is transmitted -- #
    if args_f is None:
        # -- Create all argument dictionaries that are used for function calls to make it more generic -- #
        basic_exts = {'unpack_data': True, 'deterministic': False, 'fp16': True, # Every trainer needs those args
                      'save_interval': 1, 'identifier': default_plans_identifier,
                      'tasks_list_with_char': copy.deepcopy(tasks_list_with_char),
                      'use_progress': False}
        args_f = {'nnUNetTrainerMultiHead': basic_exts}

    # -- Store the trainers for each network and extension to test the training on a pre trained method afterwards -- #
    base_trainers = dict()
    base_heads = torch.nn.ModuleDict()

    # -- Start training for every network and trainer every task conventionally, nothing special -- #
    all_tasks = copy.deepcopy(tasks)

    # -- Define the Error that is used to escape the network loop when in extensions loop -- #
    # -- This is necessary, since training 3d_lowres might not work for every dataset -- #
    # -- Details: https://stackoverflow.com/questions/1859072/python-continuing-to-next-iteration-in-outer-loop -- #
    skip_net = SkipNetwork()

    # -- Loop through the networks and train using each network -- #
    for network in networks:
        try: # Catch the skip_net Error
            # -- Loop through the different extension trainers and train for each trainer -- #
            for extension, trainer_to_use in ext_map.items():
                # -- Refresh all nnUNet modules as well as Lifelong-nnUNet modules -- #
                refresh_imports('nnunet')

                # -- Perform two iterations, one with direct sequence training and one with a sort of restoring and continuing -- #
                for i in range(3):
                    # -- Get the current timestamp to claculate the correct runtime -- #
                    start_train_time = time()

                    # -- Set the tasks given the loop index, ie. if we use a pre-trained model or not -- #
                    if i == 1:
                        # -- Set train_tasks to last task if we do use the pre-trained models -- #
                        train_tasks = tasks[-1:]
                        # -- Extract the pre-trained model path based on the network and extension -- #
                        prev_trainer_path, already_trained_on = base_trainers[str(network) + str(extension)]
                        # -- Set val_metrics_should_exist to False since it does not exist at this time --> we do not make a restoring -- #
                        # -- We only use a previous trainer but for the new trainer no results exist -- #
                        already_trained_on[str(fold)]['val_metrics_should_exist'] = False
                        # -- Set the running_task_list correctly -- #
                        running_task_list = already_trained_on[str(fold)]['finished_training_on'][:]
                    elif i == 2:
                        # -- Update the log file -- #
                        log_file = print_to_log_file(log_file, output_folder, 'pytest_log', \
                            "Start training with network \'{}\' and trainer \'nnUNetTrainerV2\' for task \'{}\'".format(network, all_tasks[0]))
                        
                        # -- Train first on nnUNetTrainerV2 to use this in the upcoming loop -- #
                        # -- Extract the corresponding informations to create a new trainer -- #
                        if network == '3d_lowres':
                            try:
                                # -- Extract the configurations -- #
                                plans_file, output_folder_name, dataset_directory, batch_dice, stage, \
                                trainer_class = nn_get_default_configuration(network, all_tasks[0], 'nnUNetTrainerV2', default_plans_identifier)
                            except RuntimeError:
                                # -- 3d_lowres does not work because the patch_size is close to image size -- #
                                # -- only 3d_fullres work then, see https://github.com/MIC-DKFZ/nnUNet/issues/488 -- #
                                raise skip_net
                        else:   # --> Should always work for every other network
                            # -- Extract the configurations -- #
                            plans_file, output_folder_name, dataset_directory, batch_dice, stage, \
                            trainer_class = nn_get_default_configuration(network, all_tasks[0], 'nnUNetTrainerV2', default_plans_identifier)
                            
                        # -- Change the output_folder_name to our new path -- #
                        output_folder_name = output_folder_name.replace(old_network_training_output_dir, network_training_output_dir)
                        # -- Create a trainer based on the extracted trainer_class -- #
                        trainer = trainer_class(plans_file, fold, output_folder=output_folder_name, dataset_directory=dataset_directory,
                                                batch_dice=batch_dice, stage=stage, unpack_data=True,
                                                deterministic=False, fp16=True)
                        # -- Change the max number of epochs to 1, otherwise this will train 1000 epochs -- #
                        trainer.max_num_epochs = 1
                        # -- Initialize and train on the first task for one epoch using the conventional nnU-Net -- #
                        trainer.initialize(True)
                        trainer.run_training()
                        # -- Evaluate and perform validation on the trainer -- #
                        trainer.network.eval()
                        trainer.validate(save_softmax=False, validation_folder_name="validation_raw",
                                        run_postprocessing_on_folds=True, overwrite=True)
                        # -- Set the tasks correctly to loop through and train using the trained network as a base -- #
                        # -- Set trainer as prev_trainer_path so it can be used in the loop and remove the trainer -- #
                        prev_trainer_path = trainer.output_folder
                        # -- Use the nnUNetTrainerV2 trained path as base and continue from there -- #
                        # -- Set train_tasks to last task if we do use the pre-trained models -- #
                        train_tasks = tasks[1:]
                        # -- Set the running_task_list correctly -- #
                        running_task_list = [all_tasks[0]]
                        del trainer
                        # -- Update the log file -- #
                        execution_time = time() - start_train_time
                        log_file = print_to_log_file(log_file, output_folder, 'pytest_log', \
                                "This execution time for training the network \'{}\' with trainer \'{}\' took {:.2f} seconds ({}).\n".format(network, extension, execution_time, time_mod.strftime('%H:%M:%S', time_mod.gmtime(execution_time))))
                        # -- Reset the start_train_time since we do not continue with next element but keep in this loop -- #
                        start_train_time = time()
                    else:
                        # -- Set train_tasks to first n-1 tasks if we do not use the pre-trained models -- #
                        train_tasks = tasks[:-1]
                        # -- Set pre-trained model path and already_trained_on to None when we do not use a pre-trained model -- #
                        prev_trainer_path, already_trained_on = None, None
                        # -- Set the running_task_list correctly -- #
                        running_task_list = list()

                    # -- Loop through the tasks and train for each task the (finished) model -- #
                    for idx_task, t in enumerate(train_tasks):
                        # -- Update the log file given the current state of training -- #
                        if i == 0:
                            # -- Update the log file -- #
                            log_file = print_to_log_file(log_file, output_folder, 'pytest_log', \
                                "Start training with network \'{}\' and trainer \'{}\' for task \'{}\'".format(network, trainer_to_use.__name__, t))
                        else:
                            # -- Update the log file -- #
                            log_file = print_to_log_file(log_file, output_folder, 'pytest_log', \
                                "Start training with network \'{}\' and trainer \'{}\' for task \'{}\' using the just trained and existing network as a foundation to continue with training".format(network, trainer_to_use.__name__, t))
                        # -- Update running task list and create running task which are all (trained tasks and current task joined) for output folder name -- #
                        running_task_list.append(t)
                        running_task = join_texts_with_char(running_task_list, char_to_join_tasks)

                        # -- Train first on nnUNetTrainerV2 to use this in the upcoming loop -- #
                        # -- Extract the corresponding informations to create a new trainer -- #
                        if network == '3d_lowres':
                            try:
                                # -- Extract the configurations -- #
                                plans_file, output_folder_name, dataset_directory, batch_dice, stage, \
                                trainer_class = get_default_configuration(network, t, running_task, trainer_to_use.__name__,
                                                      tasks_joined_name, default_plans_identifier, extension_type=extension)
                            except RuntimeError:
                                # -- 3d_lowres does not work because the patch_size is close to image size -- #
                                # -- only 3d_fullres work then, see https://github.com/MIC-DKFZ/nnUNet/issues/488 -- #
                                raise skip_net
                        else:   # --> Should always work for every other network
                            # -- Extract the configurations -- #
                            plans_file, output_folder_name, dataset_directory, batch_dice, stage, \
                            trainer_class = get_default_configuration(network, t, running_task, trainer_to_use.__name__,
                                                  tasks_joined_name, default_plans_identifier, extension_type=extension)
                        
                        # -- Change the output_folder_name to our new path -- #
                        output_folder_name = output_folder_name.replace(old_network_training_output_dir, network_training_output_dir)
                        
                        # -- Check first that the trainer class does exist and is not None -- #
                        if trainer_class is None:
                            assert False,\
                                "Could not find trainer class \'{}\' in nnunet_ext.training.network_training although it should exist.".format(trainer_to_use.__name__)
                        
                        # -- When this is the first task, the trainer is not intialized yet, so do it -- #
                        if idx_task == 0:
                            # -- Set the trainer with corresponding argument --> can only be an extension from here on -- #
                            trainer = trainer_class(split, all_tasks[0], plans_file, fold, output_folder=output_folder_name, dataset_directory=dataset_directory,\
                                                    batch_dice=batch_dice, stage=stage, extension=extension, mixed_precision=True,\
                                                    already_trained_on=already_trained_on, **(args_f[trainer_class.__name__]))
                            # -- Initialize the trainer with the dataset, task, fold, optimizer, etc. -- #
                            trainer.initialize(True, num_epochs=1, prev_trainer_path=prev_trainer_path)

                            # -- Store the current trained head when trained with conventional nnUNet first -- #
                            # -- During the loop below this only works for extensional network as a base so this overwrites it -- #
                            if i == 2:
                                base_heads[all_tasks[0]] = copy.deepcopy(trainer.mh_network.heads[all_tasks[0]])

                        # -- Create a copy from the model before training to compare if it updated itself -- #
                        trainer.mh_network.add_new_task(t)  # Add the task before trying to acess it
                        backup_head = copy.deepcopy(trainer.mh_network.heads[t])

                        # -- Check if the building of a trainer using a pre-trained network worked as expected -- #
                        if i != 0:
                            assert equal_models(trainer.mh_network.heads[all_tasks[0]], base_heads[all_tasks[0]]),\
                                "After restoring a trainer that is used as a base, the existing head states should not have changed."

                        # -- Train the trainer for the task t -- #
                        trainer.run_training(task=t, output_folder=output_folder_name)

                        # -- Create a copy from the model/head after training to compare if it updated itself -- #
                        curr_network = copy.deepcopy(trainer.network)
                        curr_head = copy.deepcopy(trainer.mh_network.heads[t])

                        # -- t should be in the head now -- #
                        assert t in trainer.mh_network.heads, "After training an unknown task \'{}\', it should be in the head afterwards.".format(t)
                        
                        # -- Check that the state dict changed after training one epoch -- #
                        assert not equal_models(backup_head, curr_head),\
                            "The state_dicts in \'{}\' are identical after training although they should not be.".format(t)

                        # -- Evaluate the trainers network -- #
                        trainer.network.eval()
                        # -- Perform validation using the trainer -- #
                        trainer.validate(save_softmax=False, validation_folder_name="validation_raw",
                                        run_postprocessing_on_folds=True, overwrite=True)

                        # -- Copy the trainer for saving the relevant parts in case of a training on a pre-trained trainer -- #
                        copy_trainer = trainer
                        
                        # -- Save the trainer head if it is the first task, so we can check that nothing changes after a second train of second head -- #
                        if idx_task == 0 and i == 0:    # Do this only once for each outer loop
                            # -- Store the current trained head if that is the first task for any network -- #
                            base_heads[all_tasks[0]] = copy.deepcopy(trainer.mh_network.heads[all_tasks[0]])

                        # --------------------- #
                        # -- Perform testing -- #
                        # --------------------- #
                        # -- Check if this is a second task, that both tasks are in the head and have different state_dicts -- #
                        assert (np.array(list(trainer.mh_network.heads.keys())) == np.array(running_task_list)).all(),\
                            "The tasks the trainer should have been trained on ({}) do not map with the ones in the head ({}).".format(list(running_task_list, trainer.mh_network.heads.keys()))
                        
                        # -- Check that the current state dict entry is different from the first one -- #
                        if idx_task != 0 or idx_task == len(train_tasks)-1 and i != 0:
                            assert not equal_models(trainer.mh_network.heads[all_tasks[0]], curr_head),\
                                "The state_dicts in \'{}\' and \'{}\' are identical although they should not be.".format(all_tasks[0], t)
                            
                        # -- Check that the correct task is activated -- #
                        assert trainer.mh_network.active_task == t, "The current task for training \'{}\' is not activated in the trainers Multi Head Network.".format(t)
                        
                        # -- Check that the current network in the trainer and the assembled model in the Multi Head Network are equal -- #
                        assert equal_models(trainer.mh_network.assemble_model(t), curr_network),\
                            "The state_dicts for task \'{}\' and the corresponding activated head are not identical although they should.".format(t)
                        
                        # -- Check that the val_metrics file does exist and is not empty -- #
                        assert os.path.isfile(join(output_folder_name, 'fold_'+str(fold), 'val_metrics.json')),\
                            "The val_metrics.json file that should have been generated at \'{}\' does not exist for task \'{}\'.".format(join(output_folder_name, 'val_metrics.json'), t)
                        try:
                            val = load_json(join(output_folder_name, 'fold_'+str(fold), 'val_metrics.json'))
                            assert val is not None and len(val) > 0,\
                                "The val_metrics.json file is either None or empty for task \'{}\' which it really should not.".format(t)
                            del val
                        except Exception as e:
                            if not isinstance(e, AssertionError):
                                assert False, "An error occured by trying to load the val_metrics.json file for task \'{}\' which is unfortunate.".format(t)
                            raise e

                        # -- Check that the already_trained_on file does exist and is not empty -- #
                        trained_on_path = os.path.dirname(os.path.dirname(os.path.realpath(output_folder_name)))
                        assert os.path.isfile(join(trained_on_path, extension+'_trained_on.json')),\
                            "The {}_trained_on.json file that should have been generated at \'{}\' does not exist for trainer \'{}\'.".format(extension, join(trained_on_path, extension+'_trained_on.json'), trainer_to_use.__name__)
                        try:
                            tr_on = load_json(join(trained_on_path, extension+'_trained_on.json'))
                            assert tr_on is not None and len(tr_on) > 0,\
                                "The {}_trained_on.json file is either None or empty for trainer \'{}\' which it really should not.".format(join(trained_on_path, extension+'_trained_on.json'), trainer_to_use.__name__)
                            del tr_on
                        except Exception as e:
                            if not isinstance(e, AssertionError):
                                assert False, "An error occured by trying to load the {}_trained_on.json file for trainer \'{}\' which is unfortunate.".format(join(trained_on_path, extension+'_trained_on.json'), trainer_to_use.__name__)
                        
                        # -- For the second task, ensure that restoring works --> only necessary for the second -- #
                        # -- task or when using pre-trained model as base -- #
                        if idx_task != 0 and i == 0 or idx_task == len(train_tasks)-1 and i != 0:
                            # -- Check that after training a the second/nth task, the first task is unchanged -- #
                            # -- For this use backup_head from previous loop and compare the state_dicts -- #
                            assert equal_models(trainer.mh_network.heads[all_tasks[0]], base_heads[all_tasks[0]]),\
                                "The state_dicts for the same task \'{}\' after a train of another task are not identical although they should.\n".format(all_tasks[0]) +\
                                "When training on a different task, the previous task weights should not have changed."

                            # -- Add a task to the trainer and try to load the checkpoint --> should certainly fail because the structure changed -- #
                            try:
                                trainer.mh_network.add_new_task('doomed_to_fail')
                                trainer.load_latest_checkpoint()
                                raise RuntimeError
                            except Exception:
                                if isinstance(Exception, RuntimeError):
                                    assert False, "The restoring surprisingly worked, although the structure has been changed before loading the checkpoint."
                            
                            # -- Remove the task again -- #
                            del trainer.mh_network.heads['doomed_to_fail']

                            # -- Delete the current task in the head and create a new head with the same name -- #
                            # -- This head has then initialized weights --> restore and check that they are as expected -- #
                            # -- ie. they should be equal to the state_dict before we deleted the head from the network -- #
                            del trainer.mh_network.heads[t]                     # Delete the head
                            trainer.mh_network.add_new_task(t)                  # Add a new head (different weights)
                            trainer.load_latest_checkpoint()                    # Restore the model from latest checkpoint
                            assert equal_models(trainer.mh_network.assemble_model(t), curr_network),\
                                "The state_dicts for task \'{}\' and the corresponding restored model are not identical although they should.".format(t)
                            del curr_network

                        # -- Do this test at the end, since this will modify the network before an error occurs -- #
                        # -- and since we catch the error, the trainer_class has the changes present --> would lead -- #
                        # -- to error if we train after that test with a messed up split/head modules -- #
                        # -- Test when using pre-trained network that the split can not be changed -- #
                        if idx_task == len(train_tasks)-1 and i != 0:
                            # -- Provide a different split as before and expect an error -- #
                            trainer_fail = trainer_class('tu', all_tasks[0], plans_file, fold, output_folder=output_folder_name, dataset_directory=dataset_directory,\
                                                        batch_dice=batch_dice, stage=stage, extension=extension, mixed_precision=True,\
                                                        already_trained_on=already_trained_on, **(args_f[trainer_class.__name__]))
                            try:
                                # -- Initialize the trainer with the dataset, task, fold, optimizer and wrong split --> should fail -- #
                                trainer_fail.initialize(True, num_epochs=1, prev_trainer_path=prev_trainer_path)
                                raise RuntimeError
                            except Exception:
                                if isinstance(Exception, RuntimeError):
                                    assert False, "When trying to split on a different layer as the checkpoint an error should be thrown."
                            
                    # -- Check again that in the current models head all tasks are present -- #
                    assert (np.array(list(trainer.mh_network.heads.keys())) == np.array(running_task_list)).all(),\
                            "The tasks the trainer should have been trained on ({}) do not map with the ones in the head ({}).".format(tasks, list(trainer.mh_network.heads.keys()))
                    del trainer

                    # -- Remove the trainer but keep a copy for the next test once this loop is done -- #
                    base_trainers[str(network) + str(extension)] = (copy_trainer.output_folder, copy_trainer.already_trained_on)
                    del copy_trainer

                    # -- Update the log file -- #
                    execution_time = time() - start_train_time
                    log_file = print_to_log_file(log_file, output_folder, 'pytest_log',\
                            "This execution time for training the network \'{}\' with trainer \'{}\' took {:.2f} seconds ({}).\n".format(network, extension, execution_time, time_mod.strftime('%H:%M:%S', time_mod.gmtime(execution_time))))

                # -- Remove the trainer_class -- #
                del trainer_class
        # -- Catch only the SkipNetwork error and continue --> only thrown when using 3d_lowres -- #
        except SkipNetwork:
            # -- Write this into the log file and move on with next extension (do this until fionished with network) -- #
            log_file = print_to_log_file(log_file, output_folder, 'pytest_log',\
                "Training on \'3d_lowres\' needs to be skipped, since the initialization for the first task \'{}\' does not work.\n".format(all_tasks[0]) + 
                "See: https://github.com/MIC-DKFZ/nnUNet/issues/488 for more details.\n")
            continue

    # ----------------------- #
    # -- Clean the mess up -- #
    # ----------------------- #
    # -- Move the preprocessed, cropped and raw data with the new task ids from the original paths to our temporary location -- #
    tasks = ['Task-11_Hippocampus', 'Task-33_Prostate', 'Task-22_Heart']
    # -- Loop through the new tasks and move them from cropped, preprocessed and raw_data to temp folder -- #
    for task in tasks:
        # -- Move from raw_data -- #
        move_dir(join(old_nnUNet_raw_data, task), nnUNet_raw_data)
        # -- Update the log file -- #
        log_file = print_to_log_file(log_file, output_folder, 'pytest_log',\
            "Moved raw data from \'{}\' to \'{}\'".format(join(old_nnUNet_raw_data, task), join(nnUNet_raw_data, task)))
        # -- Move from cropped_data -- #
        move_dir(join(old_nnUNet_cropped_data, task), nnUNet_cropped_data)
        # -- Update the log file -- #
        log_file = print_to_log_file(log_file, output_folder, 'pytest_log',\
            "Moved cropped data from \'{}\' to \'{}\'".format(join(old_nnUNet_cropped_data, task), join(nnUNet_cropped_data, task)))
        # -- Move from preprocessed_data -- #
        move_dir(join(old_preprocessing_output_dir, task), preprocessing_output_dir)
        # -- Update the log file -- #
        log_file = print_to_log_file(log_file, output_folder, 'pytest_log',\
            "Moved preprocessed data from \'{}\' to \'{}\'".format(join(old_preprocessing_output_dir, task), join(preprocessing_output_dir, task)))

    # -- Delete the folder with the generated folder, since at this point no test did not failed -- #
    delete_dir_con(base)
    
    # -- Update the log file for the last time -- #
    execution_time = time() - start_time
    log_file = print_to_log_file(log_file, output_folder, 'pytest_log',\
            "The execution time for all tests took {:.2f} seconds ({})\n".format(execution_time, time_mod.strftime('%H:%M:%S', time_mod.gmtime(execution_time))))


if __name__ == "__main__":
    # -- Block all prints that are done during testing which are no errors but done in calling functions -- #
    sys.stdout = open(os.devnull, 'w')

    # -- Run the test suite and catch any error, delete the folder and raise this error -- #
    try:
        # -- Run the test suite -- #
        test_multi_head_trainer()
    except Exception as e:  # Error occured or test failed
        # -- Delete the generated data -- #
        delete_specified_task(False, test_data=True, task_ids=list())
        # -- Enable the prints again -- #
        sys.stdout = sys.__stdout__
        # -- Raise the error -- #
        print("An Error occured or a test failed, for more information see the log file at \'{}\'.".format(old_network_training_output_dir_base))
        raise e

    # -- Ensure that the data is really removed -- #
    delete_specified_task(False, test_data=True, task_ids=list())