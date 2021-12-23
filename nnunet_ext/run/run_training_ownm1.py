#########################################################################################################
#----------This class represents the Training of networks using the extended nnUNet POD ----------------#
#----------training version. This runs the Training for the POD approach.-------------------------------#
#########################################################################################################

from nnunet_ext.run.run_training import run_training

def main():
    r"""Run training for OwnMethod1 Trainer.
    """
    run_training(extension='ownm1')

if __name__ == "__main__":
    main()