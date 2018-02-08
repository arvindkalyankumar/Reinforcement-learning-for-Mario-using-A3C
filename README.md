# Reinforcement-learning-for-Mario-using-A3C

Packages : PyTorch, OpenCV and Gym 

To set up the environment please use this package https://github.com/ppaquette/gym-super-mario.(Beware, this doesn't work using the Github instructions.)

To set it up properly on the latest gym package follow the instructions in this link : https://github.com/ppaquette/gym-super-mario/issues/6 with a small alteration to the .../virtualenv/lib/python3._/site-packages/gym/envs/__init__.py file. 
 
To run the A3C architecture run python3 train-mario.py (Add arguments based on your system). It doesn't work on python2.7 as multiprocessing works only on python 3.5+ versions on PyTorch. A3C is the model which trains within 24 hours, using 7 training processes, GPU with 8+ GB GPU RAM, 20+GB RAM. 

You can change the arguments to have it train faster using more processes (ARGS: --num-process <8+> --non-sample<2+>).
