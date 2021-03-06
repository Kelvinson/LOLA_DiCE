# coding: utf-8

import numpy as np
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
from copy import deepcopy

class Hp():
    def __init__(self):
        self.lr_out = 0.2
        self.lr_in = 0.3
        self.lr_v = 0.1
        self.gamma = 0.96
        self.n_update = 200
        self.len_rollout = 150
        self.batch_size = 128
        self.use_baseline = True
        self.seed = 42

hp = Hp()

def magic_box(x):
    return torch.exp(x - x.detach())

class IPD():
    def __init__(self, batch_size):
        self.batch_size = batch_size
        self.M = np.array([[-2,0],[-3,-1]])
        self.S = np.array([[1,2],[3,4]])

    def reset(self):
        return [np.zeros(self.batch_size)]*2

    def step(self, actions1, actions2):
        rewards1 = self.M[actions1, actions2]
        rewards2 = self.M[actions2, actions1]
        states1 = self.S[actions1, actions2]
        states2 = self.S[actions2, actions1]
        return rewards1, rewards2, states1, states2

ipd = IPD(hp.batch_size)

class Memory():
    def __init__(self):
        self.self_logprobs = []
        self.other_logprobs = []
        self.values = []
        self.rewards = []

    def add(self, lp, other_lp, v, r):
        self.self_logprobs.append(lp)
        self.other_logprobs.append(other_lp)
        self.values.append(v)
        self.rewards.append(r)

    def dice_objective(self):
        self_logprobs = torch.stack(self.self_logprobs, dim=1)
        other_logprobs = torch.stack(self.other_logprobs, dim=1)
        values = torch.stack(self.values, dim=1)
        rewards = torch.stack(self.rewards, dim=1)

        # apply discount:
        cum_discount = torch.cumprod(hp.gamma * torch.ones(*rewards.size()), dim=1)/hp.gamma
        discounted_rewards = rewards * cum_discount
        discounted_values = values * cum_discount

        # stochastics nodes involved in rewards dependencies:
        dependencies = torch.cumsum(self_logprobs + other_logprobs, dim=1)

        # logprob of each stochastic nodes:
        stochastic_nodes = self_logprobs + other_logprobs

        # dice objective:
        dice_objective = torch.mean(torch.sum(magic_box(dependencies) * discounted_rewards, dim=1))

        if hp.use_baseline:
            # variance_reduction:
            baseline_term = torch.mean(torch.sum((1 - magic_box(stochastic_nodes)) * discounted_values, dim=1))
            dice_objective = dice_objective + baseline_term

        return -dice_objective # want to minimize -objective

    def value_loss(self):
        values = torch.stack(self.values, dim=1)
        rewards = torch.stack(self.rewards, dim=1)
        return torch.mean((rewards - values)**2)

def act(batch_states, theta, values):
    batch_states = torch.from_numpy(batch_states).long()
    probs = torch.sigmoid(theta)[batch_states]
    actions = (torch.rand(*probs.size())>probs).long().numpy()
    torch_actions = torch.from_numpy(actions).float()
    log_probs_actions = torch.log(torch_actions*(1-probs) + (1-torch_actions)*probs)
    return actions, log_probs_actions, values[batch_states]

def get_gradient(objective, theta):
    # create differentiable gradient for 2nd orders:
    grad_objective = torch.autograd.grad(objective, (theta), create_graph=True)[0]
    return grad_objective

def step(theta1, theta2, values1, values2):
    # just to evaluate progress:
    s1, s2 = ipd.reset()
    score1 = 0
    score2 = 0
    for t in range(hp.len_rollout):
        a1, lp1, v1 = act(s1, theta1, values1)
        a2, lp2, v2 = act(s2, theta2, values2)
        r1, r2, s1, s2 = ipd.step(a1, a2)
        # cumulate scores
        score1 += np.mean(r1)/float(hp.len_rollout)
        score2 += np.mean(r2)/float(hp.len_rollout)
    return score1, score2

class Agent():
    def __init__(self):
        # init theta and its optimizer
        self.theta = nn.Parameter(torch.zeros(5, requires_grad=True))
        self.theta_optimizer = torch.optim.Adam((self.theta,),lr=hp.lr_out)
        # init values and its optimizer
        self.values = nn.Parameter(torch.zeros(5, requires_grad=True))
        self.value_optimizer = torch.optim.Adam((self.values,),lr=hp.lr_v)

    def theta_update(self, objective):
        self.theta_optimizer.zero_grad()
        objective.backward(retain_graph=True)
        self.theta_optimizer.step()

    def value_update(self, loss):
        self.value_optimizer.zero_grad()
        loss.backward()
        self.value_optimizer.step()

    def in_lookahead(self, other_theta, other_values):
        s1, s2 = ipd.reset()
        other_memory = Memory()
        for t in range(hp.len_rollout):
            a1, lp1, v1 = act(s1, self.theta, self.values)
            a2, lp2, v2 = act(s2, other_theta, other_values)
            r1, r2, s1, s2 = ipd.step(a1, a2)
            other_memory.add(lp2, lp1, v2, torch.from_numpy(r2).float())

        other_objective = other_memory.dice_objective()
        grad = get_gradient(other_objective, other_theta)
        return grad

    def out_lookahead(self, other_theta, other_values):
        s1, s2 = ipd.reset()
        memory = Memory()
        for t in range(hp.len_rollout):
            a1, lp1, v1 = act(s1, self.theta, self.values)
            a2, lp2, v2 = act(s2, other_theta, other_values)
            r1, r2, s1, s2 = ipd.step(a1, a2)
            memory.add(lp1, lp2, v1, torch.from_numpy(r1).float())

        # update self theta
        objective = memory.dice_objective()
        self.theta_update(objective)
        # update self value:
        v_loss = memory.value_loss()
        self.value_update(v_loss)

def play(agent1, agent2, n_lookaheads):
    joint_scores = []
    print("start iterations with", n_lookaheads, "lookaheads:")
    for update in range(hp.n_update):
        # copy other's parameters:
        theta1_ = torch.tensor(agent1.theta.detach(), requires_grad=True)
        values1_ = torch.tensor(agent1.values.detach(), requires_grad=True)
        theta2_ = torch.tensor(agent2.theta.detach(), requires_grad=True)
        values2_ = torch.tensor(agent2.values.detach(), requires_grad=True)

        for k in range(n_lookaheads):
            # estimate other's gradients from in_lookahead:
            grad2 = agent1.in_lookahead(theta2_, values2_)
            grad1 = agent2.in_lookahead(theta1_, values1_)
            # update other's theta
            theta2_ = theta2_ - hp.lr_in * grad2
            theta1_ = theta1_ - hp.lr_in * grad1

        # update own parameters from out_lookahead:
        agent1.out_lookahead(theta2_, values2_)
        agent2.out_lookahead(theta1_, values1_)

        # evaluate progress:
        score = step(agent1.theta, agent2.theta, agent1.values, agent2.values)
        joint_scores.append(0.5*(score[0] + score[1]))

        # print
        if update%10==0 :
            p1 = [p.item() for p in torch.sigmoid(agent1.theta)]
            p2 = [p.item() for p in torch.sigmoid(agent2.theta)]
            print('update', update, 'score (%.3f,%.3f)' % (score[0], score[1]) , 'policy (agent1) = {%.3f, %.3f, %.3f, %.3f, %.3f}' % (p1[0], p1[1], p1[2], p1[3], p1[4]),' (agent2) = {%.3f, %.3f, %.3f, %.3f, %.3f}' % (p2[0], p2[1], p2[2], p2[3], p2[4]))

    return joint_scores

# plot progress:
if __name__=="__main__":

    colors = ['b','c','m','r']

    for i in range(4):
        torch.manual_seed(hp.seed)
        scores = play(Agent(), Agent(), i)
        plt.plot(scores, colors[i], label=str(i)+" lookaheads")

    plt.legend()
    plt.xlabel('rollouts')
    plt.ylabel('joint score')
    plt.show()
