#!/usr/bin/env python
import pickle
import os
import collections
import numpy as np
import math
import encoder_decoder_c4 as ed
from connect_board import board as c_board
import copy
import torch
import torch.multiprocessing as mp
from alpha_net_c4 import ConnectNet
import datetime
import logging
from tqdm import tqdm

logging.basicConfig(format='%(asctime)s [%(levelname)s]: %(message)s', \
                    datefmt='%m/%d/%Y %I:%M:%S %p', level=logging.INFO)
logger = logging.getLogger(__file__)

def save_as_pickle(filename, data):
    completeName = os.path.join("./datasets/",\
                                filename)
    with open(completeName, 'wb') as output:
        pickle.dump(data, output)

def load_pickle(filename):
    completeName = os.path.join("./datasets/",\
                                filename)
    with open(completeName, 'rb') as pkl_file:
        data = pickle.load(pkl_file)
    return data

class UCTNode():
    def __init__(self, game, move, parent=None):
        self.game = game # state s
        self.move = move # action index
        self.is_expanded = False
        self.parent = parent  
        self.children = {}
        self.child_priors = np.zeros([7], dtype=np.float32)
        self.child_total_value = np.zeros([7], dtype=np.float32)
        self.child_number_visits = np.zeros([7], dtype=np.float32)
        self.action_idxes = []
        
    @property
    def number_visits(self):
        return self.parent.child_number_visits[self.move]

    @number_visits.setter
    def number_visits(self, value):
        self.parent.child_number_visits[self.move] = value
    
    @property
    def total_value(self):
        return self.parent.child_total_value[self.move]
    
    @total_value.setter
    def total_value(self, value):
        self.parent.child_total_value[self.move] = value
    
    def child_Q(self):
        return self.child_total_value / (1 + self.child_number_visits)
    
    def child_U(self):
        return math.sqrt(self.number_visits) * (
            abs(self.child_priors) / (1 + self.child_number_visits))
    
    def best_child(self):
        if self.action_idxes != []:
            bestmove = self.child_Q() + self.child_U()
            bestmove = self.action_idxes[np.argmax(bestmove[self.action_idxes])]
        else:
            bestmove = np.argmax(self.child_Q() + self.child_U())
        return bestmove
    
    def select_leaf(self):
        current = self
        while current.is_expanded:
          best_move = current.best_child()
          current = current.maybe_add_child(best_move)
        return current
    
    def add_dirichlet_noise(self,action_idxs,child_priors):
        valid_child_priors = child_priors[action_idxs] # select only legal moves entries in child_priors array
        valid_child_priors = 0.75*valid_child_priors + 0.25*np.random.dirichlet(np.zeros([len(valid_child_priors)], \
                                                                                          dtype=np.float32)+192)
        child_priors[action_idxs] = valid_child_priors
        return child_priors
    
    def expand(self, child_priors):
        self.is_expanded = True
        action_idxs = self.game.actions(); c_p = child_priors
        if action_idxs == []:
            self.is_expanded = False
        self.action_idxes = action_idxs
        c_p[[i for i in range(len(child_priors)) if i not in action_idxs]] = 0.000000000 # mask all illegal actions
        if self.parent.parent == None: # add dirichlet noise to child_priors in root node
            c_p = self.add_dirichlet_noise(action_idxs,c_p)
        self.child_priors = c_p
    
    def decode_n_move_pieces(self,board,move):
        board.drop_piece(move)
        return board
            
    def maybe_add_child(self, move):
        if move not in self.children:
            copy_board = copy.deepcopy(self.game) # make copy of board
            copy_board = self.decode_n_move_pieces(copy_board,move)
            self.children[move] = UCTNode(
              copy_board, move, parent=self)
        return self.children[move]
    
    def backup(self, value_estimate: float):
        current = self
        while current.parent is not None:
            current.number_visits += 1
            if current.game.player == 1: # same as current.parent.game.player = 0
                current.total_value += (1*value_estimate) # value estimate +1 = O wins
            elif current.game.player == 0: # same as current.parent.game.player = 1
                current.total_value += (-1*value_estimate)
            current = current.parent
        
class DummyNode(object):
    def __init__(self):
        self.parent = None
        self.child_total_value = collections.defaultdict(float)
        self.child_number_visits = collections.defaultdict(float)

def UCT_search(game_states, num_reads, net):
    batch_size = len(game_states)
    roots = [UCTNode(game_states[i], move=None, parent=DummyNode()) for i in range(batch_size)]
    check_mates = [False for j in range(batch_size)]

    for i in range(num_reads):
        leaves = []
        boards = []
        for j in range(batch_size):
          leaf = roots[j].select_leaf()
          leaves.append(leaf)
          encoded_s = ed.encode_board(leaf.game) 
          encoded_s = encoded_s.transpose(2,0,1)
          boards.append(encoded_s)
        stacked_boards = np.stack(boards, axis=0)
        if torch.cuda.is_available():
            stacked_boards = torch.from_numpy(stacked_boards).float().cuda()
        else:
            stacked_boards = torch.from_numpy(stacked_boards).float()
        child_priors, value_estimates = net(stacked_boards)
        child_priors = child_priors.detach().cpu().numpy()
        value_estimates = value_estimates.detach().cpu().numpy()
        for j in range(batch_size):
          if check_mates[j]:
            continue;
          leaf = leaves[j]
          if leaf.game.check_winner() == True or leaf.game.actions() == []: # if somebody won or draw
              leaf.backup(value_estimates[j][0])
              check_mates[j] = True
              continue
          leaf.expand(child_priors[j]) # need to make sure valid moves
          leaf.backup(value_estimates[j][0])
    return roots

def do_decode_n_move_pieces(board,move):
    board.drop_piece(move)
    return board

def get_policy(root, temp=1):
    #policy = np.zeros([7], dtype=np.float32)
    #for idx in np.where(root.child_number_visits!=0)[0]:
    #    policy[idx] = ((root.child_number_visits[idx])**(1/temp))/sum(root.child_number_visits**(1/temp))
    if sum(root.child_number_visits) == 0:
      return None
    else:
        return ((root.child_number_visits)**(1/temp))/sum(root.child_number_visits**(1/temp))

def MCTS_self_play(connectnet, num_games, start_idx, cpu, args, iteration, batch_size):
    logger.info("[CPU: %d]: Starting MCTS self-play..." % cpu)
    
    if not os.path.isdir("./datasets/iter_%d" % iteration):
        if not os.path.isdir("datasets"):
            os.mkdir("datasets")
        os.mkdir("datasets/iter_%d" % iteration)
        
    for idxx in tqdm(range(start_idx, num_games + start_idx)):
        logger.info("[CPU: %d]: Game %d, batch size %d" % (cpu, idxx, batch_size))
        current_boards = [c_board() for i in range(batch_size)]
        dataset = [] # to get state, policy, value for neural network training
        states = []
        check_mates = [False for i in range(batch_size)]
        value = 0
        move_counts = [0 for i in range(batch_size)]
        finished_games = 0
        while finished_games < batch_size:
            for i in range(batch_size):
                current_board = current_boards[i]
                states.append(copy.deepcopy(current_board.current_board))
            roots = UCT_search(current_boards, 777, connectnet)
            finished_games = 0
            for i in range(batch_size):
                current_board = current_boards[i]
                if move_counts[i] < 11:
                    t = args.temperature_MCTS
                else:
                    t = 0.1
                policy = get_policy(roots[i], t)
                if not policy is None:
                  current_board = do_decode_n_move_pieces(current_board,\
                                                          np.random.choice(np.array([0,1,2,3,4,5,6]), p = policy)) # decode move and move piece(s)
                  board_state = copy.deepcopy(ed.encode_board(current_board))
                  dataset.append([board_state,policy])
                if current_board.check_winner() == True: # if somebody won
                    if current_board.player == 0: # black wins
                        value = -1
                    elif current_board.player == 1: # white wins
                        value = 1
                    finished_games += 1
                move_counts[i] += 1
            logger.info("[CPU: %d]: Finished games %d" % (cpu, finished_games))
        dataset_p = []
        for idx,data in enumerate(dataset):
            s,p = data
            if idx == 0:
                dataset_p.append([s,p,0])
            else:
                dataset_p.append([s,p,value])
        del dataset
        save_as_pickle("iter_%d/" % iteration +\
                       "dataset_iter%d_cpu%i_%i_%s" % (iteration, cpu, idxx, datetime.datetime.today().strftime("%Y-%m-%d")), dataset_p)
   
def run_MCTS(args, start_idx=0, iteration=0):
    net_to_play="%s_iter%d.pth.tar" % (args.neural_net_name, iteration)
    net = ConnectNet()
    cuda = torch.cuda.is_available()
    if cuda:
        net.cuda()
    
    if args.MCTS_num_processes > 1:
        logger.info("Preparing model for multi-process MCTS...")
        mp.set_start_method("spawn",force=True)
        net.share_memory()
        net.eval()
    
        current_net_filename = os.path.join("./model_data/",\
                                        net_to_play)
        if os.path.isfile(current_net_filename):
            checkpoint = torch.load(current_net_filename)
            net.load_state_dict(checkpoint['state_dict'])
            logger.info("Loaded %s model." % current_net_filename)
        else:
            torch.save({'state_dict': net.state_dict()}, os.path.join("./model_data/",\
                        net_to_play))
            logger.info("Initialized model.")
        
        processes = []
        if args.MCTS_num_processes > mp.cpu_count():
            num_processes = mp.cpu_count()
            logger.info("Required number of processes exceed number of CPUs! Setting MCTS_num_processes to %d" % num_processes)
        else:
            num_processes = args.MCTS_num_processes
        
        logger.info("Spawning %d processes..." % num_processes)
        with torch.no_grad():
            for i in range(num_processes):
                p = mp.Process(target=MCTS_self_play, args=(net, args.num_games_per_MCTS_process, start_idx, i, args, iteration, args.batch_size))
                p.start()
                processes.append(p)
            for p in processes:
                p.join()
        logger.info("Finished multi-process MCTS!")
    
    elif args.MCTS_num_processes == 1:
        logger.info("Preparing model for MCTS...")
        net.eval()
        
        current_net_filename = os.path.join("./model_data/",\
                                        net_to_play)
        if os.path.isfile(current_net_filename):
            checkpoint = torch.load(current_net_filename)
            net.load_state_dict(checkpoint['state_dict'])
            logger.info("Loaded %s model." % current_net_filename)
        else:
            torch.save({'state_dict': net.state_dict()}, os.path.join("./model_data/",\
                        net_to_play))
            logger.info("Initialized model.")
        
        with torch.no_grad():
            MCTS_self_play(net, args.num_games_per_MCTS_process, start_idx, 0, args, iteration, args.batch_size)
        logger.info("Finished MCTS!")
