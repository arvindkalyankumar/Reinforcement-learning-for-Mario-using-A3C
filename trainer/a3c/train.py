import os
import time
from collections import deque
import csv

import numpy as np
import cv2
from itertools import count
import torch
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable

from models.actor_critic import ActorCritic
from common.atari_wrapper import create_mario_env
from common.mario_actions import ACTIONS



def ensure_shared_grads(model, shared_model):
    for param, shared_param in zip(model.parameters(),
                                   shared_model.parameters()):
        if shared_param.grad is not None:
            return
        shared_param._grad = param.grad


def choose_action(model, state, hx, cx):
    self.eval()
    logits, _ = self.forward(s)
    prob = F.softmax(logits, dim=1).data
    m = self.distribution(prob)
    return m.sample().numpy()[0]

def train(rank, args, shared_model, counter, lock, optimizer=None, select_sample=True):
    torch.manual_seed(args.seed + rank)

    print("Process No : {} | Sampling : {}".format(rank, select_sample))

    FloatTensor = torch.cuda.FloatTensor if args.use_cuda else torch.FloatTensor
    DoubleTensor = torch.cuda.DoubleTensor if args.use_cuda else torch.DoubleTensor
    ByteTensor = torch.cuda.ByteTensor if args.use_cuda else torch.ByteTensor


    env = create_mario_env(args.env_name)
    env.seed(args.seed + rank)

    model = ActorCritic(env.observation_space.shape[0], len(ACTIONS))
    if args.use_cuda:
        model.cuda()
    if optimizer is None:
        optimizer = optim.Adam(shared_model.parameters(), lr=args.lr)

    model.train()

    state = env.reset()
    state = torch.from_numpy(state)
    done = True

    episode_length = 0
    for num_iter in count():
        
        if rank == 0:
            env.render()

            if num_iter % args.save_interval == 0 and num_iter > 0:
                print ("Saving model at :" + args.save_path)            
                torch.save(shared_model.state_dict(), args.save_path)

        if num_iter % (args.save_interval * 2.5) == 0 and num_iter > 0 and rank == 1:
            print ("Saving model for process 1 at :" + args.save_path)            
            torch.save(shared_model.state_dict(), args.save_path)
            
        # Sync with the shared model
        model.load_state_dict(shared_model.state_dict())
        if done:
            cx = Variable(torch.zeros(1, 512)).type(FloatTensor)
            hx = Variable(torch.zeros(1, 512)).type(FloatTensor)
        else:
            cx = Variable(cx.data).type(FloatTensor)
            hx = Variable(hx.data).type(FloatTensor)

        values = []
        log_probs = []
        rewards = []
        entropies = []
        reason =''
        
        for step in range(args.num_steps):
            episode_length += 1            
            state_inp = Variable(state.unsqueeze(0)).type(FloatTensor)
            value, logit, (hx, cx) = model((state_inp, (hx, cx)))
            prob = F.softmax(logit, dim=-1)
            log_prob = F.log_softmax(logit, dim=-1)
            entropy = -(log_prob * prob).sum(-1, keepdim=True)
            entropies.append(entropy)
            
            if select_sample:
                action = prob.multinomial().data
            else:
                action = prob.max(-1, keepdim=True)[1].data

            log_prob = log_prob.gather(-1, Variable(action))
            
            action_out = ACTIONS[action][0, 0]
            # print("Process: {} Action: {}".format(rank,  str(action_out)))

            state, reward, done, _ = env.step(action_out)

            if done:
                reason = 'done'
            else:
                reason = str(episode_length)

            done = done or episode_length >= args.max_episode_length
            reward = max(min(reward, 10), -10)

            with lock:
                counter.value += 1

            if done:
                episode_length = 0
                env.change_level(0)
                state = env.reset()
                print ("Process {} has completed with reason : {}.".format(rank, reason))

            env.locked_levels = [False] + [True] * 31
            state = torch.from_numpy(state)
            values.append(value)
            log_probs.append(log_prob)
            rewards.append(reward)

            if done:
                break

        R = torch.zeros(1, 1)
        if not done:
            state_inp = Variable(state.unsqueeze(0)).type(FloatTensor)
            value, _, _ = model((state_inp, (hx, cx)))
            R = value.data

        values.append(Variable(R).type(FloatTensor))
        policy_loss = 0
        value_loss = 0
        R = Variable(R).type(FloatTensor)
        gae = torch.zeros(1, 1).type(FloatTensor)
        for i in reversed(range(len(rewards))):
            R = args.gamma * R + rewards[i]
            advantage = R - values[i]
            value_loss = value_loss + 0.5 * advantage.pow(2)

            # Generalized Advantage Estimataion
            delta_t = rewards[i] + args.gamma * \
                values[i + 1].data - values[i].data
            gae = gae * args.gamma * args.tau + delta_t

            policy_loss = policy_loss - \
                log_probs[i] * Variable(gae).type(FloatTensor) - args.entropy_coef * entropies[i]

        total_loss = policy_loss + args.value_loss_coef * value_loss
        
        print ("Process {} run has completed with loss :".format(rank), total_loss.data)
        # print("Process: {} Episode: {}".format(rank,  str(episode_length)))
        optimizer.zero_grad()

        (total_loss).backward()
        torch.nn.utils.clip_grad_norm(model.parameters(), args.max_grad_norm)

        ensure_shared_grads(model, shared_model)
        optimizer.step()
    print ("Process {} closed.".format(rank))

def test(rank, args, shared_model, counter):
    torch.manual_seed(args.seed + rank)

    FloatTensor = torch.cuda.FloatTensor if args.use_cuda else torch.FloatTensor
    DoubleTensor = torch.cuda.DoubleTensor if args.use_cuda else torch.DoubleTensor
    ByteTensor = torch.cuda.ByteTensor if args.use_cuda else torch.ByteTensor

    env = create_mario_env(args.env_name)
    env.seed(args.seed + rank)

    model = ActorCritic(env.observation_space.shape[0], len(ACTIONS))
    if args.use_cuda:
        model.cuda()
    model.eval()

    state = env.reset()
    state = torch.from_numpy(state)
    reward_sum = 0
    done = True
    savefile = os.getcwd() + '/save/mario_curves.csv'
    
    title = ['Time','No. Steps', 'Total Reward', 'Episode Length']
    with open(savefile, 'a', newline='') as sfile:
        writer = csv.writer(sfile)
        writer.writerow(title)    

    start_time = time.time()

    # a quick hack to prevent the agent from stucking
    actions = deque(maxlen=4000)
    episode_length = 0
    while True:
        episode_length += 1
        ep_start_time = time.time()
        # Sync with the shared model
        if done:
            model.load_state_dict(shared_model.state_dict())
            cx = Variable(torch.zeros(1, 512), volatile=True).type(FloatTensor)
            hx = Variable(torch.zeros(1, 512), volatile=True).type(FloatTensor)

        else:
            cx = Variable(cx.data, volatile=True).type(FloatTensor)
            hx = Variable(hx.data, volatile=True).type(FloatTensor)
        

        state_inp = Variable(state.unsqueeze(0), volatile=True).type(FloatTensor)
        value, logit, (hx, cx) = model((state_inp, (hx, cx)))
        prob = F.softmax(logit, dim=-1)
        action = prob.max(-1, keepdim=True)[1].data
        
        action_out = ACTIONS[action][0, 0]
        # print("Process: Test Action: {}".format(str(action_out)))
        
        state, reward, done, _ = env.step(action_out)
        env.render()
        done = done or episode_length >= args.max_episode_length
        reward_sum += reward

        # a quick hack to prevent the agent from stucking
        actions.append(action[0, 0])
        if actions.count(actions[0]) == actions.maxlen:
            done = True

        if done:
            print("Time {}, num steps {}, FPS {:.0f}, episode reward {}, episode length {}".format(
                time.strftime("%Hh %Mm %Ss",
                              time.gmtime(time.time() - start_time)), 
                counter.value, counter.value / (time.time() - start_time),
                reward_sum, episode_length))
            
            data = [time.time() - ep_start_time,
                    counter.value, reward_sum, episode_length]
            
            with open(savefile, 'a', newline='') as sfile:
                writer = csv.writer(sfile)
                writer.writerows([data])
            
            reward_sum = 0
            episode_length = 0
            actions.clear()
            time.sleep(60)
            env.locked_levels = [False] + [True] * 31
            env.change_level(0)
            state = env.reset()

        state = torch.from_numpy(state)
