from distutils.core import setup
from Cython.Build import cythonize

setup(
    ext_modules = cythonize([
        "alpha_net_c4.py",
        "evaluator_c4.py",
        "MCTS_c4.py",
        "play_against_c4.py",
        "train_c4.py",
        "connect_board.py",
        "encoder_decoder_c4.py",
        "main_pipeline.py",
        "visualize_board_c4.py",
        ])
)
