# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:light
#     text_representation:
#       extension: .py
#       format_name: light
#       format_version: '1.5'
#       jupytext_version: 1.6.0
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# +
DAP = False

if DAP:
# #     ! pip install -r ../requirements.txt
# #     ! aws s3 cp s3://user/gc03ye/uploads/VRD/ /tmp/data/annotations --recursive --exclude "sg_dataset*"
# #     ! aws s3 cp s3://user/gc03ye/uploads/VRD/sg_dataset/sg_train_images/ /tmp/data/images/train_images --recursive
# #     ! aws s3 cp s3://user/gc03ye/uploads/VRD/sg_dataset/sg_test_images/ /tmp/data/images/test_images --recursive
# #     ! aws s3 cp s3://user/gc03ye/uploads/glove /tmp/data/word_embeddings --recursive
# #     ! aws s3 cp s3://user/gc03ye/uploads/resnet_old.pth /tmp/models/resnet_old.pth
    import torch
    path_prefix = "/tmp/data/"
    pretrained_model = torch.load(path_prefix + "models/resnet_old.pth")
else:
    import torchvision.models as models
    pretrained_model = models.resnet18(pretrained=True)
    path_prefix = "../data/VRD/"

# +
# %load_ext autoreload
# %autoreload 2

import itertools
import numpy as np
import matplotlib.pyplot as plt
import os
import pandas as pd
import pickle
import random
from scipy.stats import entropy
import seaborn as sns
import sys
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from tqdm import tqdm

sys.path.append(os.path.abspath("../activeweasul"))
from synthetic_data import SyntheticDataGenerator
from logisticregression import LogisticRegression
from discriminative_model import DiscriminativeModel
from label_model import LabelModel
from active_weasul import ActiveWeaSuLPipeline, set_seed, CustomTensorDataset
from plot import plot_probs, plot_train_loss
from experiments import process_metric_dict, plot_metrics, active_weasul_experiment, process_exp_dict, active_learning_experiment, bucket_entropy_experiment, add_baseline, add_optimal, synthetic_al_experiment
from vr_utils import load_vr_data, balance_dataset, df_drop_duplicates
from lf_utils import apply_lfs, analyze_lfs
from visualrelation import VisualRelationDataset, VisualRelationClassifier, WordEmb, FlatConcat

# -

# ### Generate data

# +
balance=False
semantic_predicates = [
        "carry",
        "cover",
        "fly",
        "look",
        "lying on",
        "park on",
        "sit on",
        "stand on",
        "ride",
#         "wear"
    ]

classify = ["sit on"]
df_train, df_test = load_vr_data(classify=classify, include_predicates=semantic_predicates, path_prefix=path_prefix, drop_duplicates=True, balance=balance, validation=False)

y_train = df_train.y.values
y_test = df_test.y.values

print("Train Relationships: ", len(df_train))
print("Test Relationships: ", len(df_test))

# +
dataset_train = VisualRelationDataset(image_dir=path_prefix + "/images/train_images", df=df_train, Y=y_train)

dl_train = DataLoader(dataset_train, shuffle=False, batch_size=256)

final_model = VisualRelationClassifier(pretrained_model, lr=1e-3, n_epochs=3, data_path_prefix="../data/", soft_labels=False)

feature_tensor_train = torch.Tensor([])

for batch_features, batch_labels in tqdm(dl_train):
    feature_tensor_train = torch.cat((feature_tensor_train, final_model.extract_concat_features(batch_features).to("cpu")))

# +
dataset_test = VisualRelationDataset(image_dir=path_prefix + "images/test_images", 
                      df=df_test,
                      Y=y_test)

dl_test = DataLoader(dataset_test, shuffle=False, batch_size=256)

feature_tensor_test = torch.Tensor([])

for batch_features, batch_labels in dl_test:
    feature_tensor_test = torch.cat((feature_tensor_test, final_model.extract_concat_features(batch_features).to("cpu")))
# -

# ### Apply labeling functions

SITON = 1
OTHER = 0


# +
def lf_siton_object(x):
    if x.subject_category == "person":
        if x.object_category in ["bench", "chair", "floor", "horse", "grass", "table"]:
            return SITON
    return OTHER

def lf_not_person(x):
    if x.subject_category != "person":
        return OTHER
    return SITON

YMIN = 0
YMAX = 1
XMIN = 2
XMAX = 3

def lf_ydist(x):
    if x.subject_bbox[YMAX] < x.object_bbox[YMAX] and x.subject_bbox[YMIN] < x.object_bbox[YMIN]:
        return SITON
    return OTHER

def lf_xdist(x):
    if x.subject_bbox[XMAX] < x.object_bbox[XMIN] or x.subject_bbox[XMIN] > x.object_bbox[XMAX]: 
        return OTHER
    return SITON

def lf_dist(x):
    if np.linalg.norm(np.array(x.subject_bbox) - np.array(x.object_bbox)) >= 100:
        return OTHER
    return SITON

def area(bbox):
    return (bbox[YMAX] - bbox[YMIN]) * (bbox[XMAX] - bbox[XMIN])

def lf_area(x):
    if area(x.subject_bbox) / area(x.object_bbox) < 0.8:
        return SITON
    return OTHER


# +
# lfs = [lf_siton_object, lf_not_person, lf_xdist, lf_area, lf_ydist]
lfs = [lf_siton_object, lf_dist, lf_area]

label_matrix = apply_lfs(df_train, lfs)
label_matrix_test = apply_lfs(df_test, lfs)
# -

analyze_lfs(label_matrix_test, df_test["y"], lfs)


# +
class_balance = np.array([1-df_train.y.mean(), df_train.y.mean()])

cliques=[[0],[1,2]]
# cliques=[[0,1],[2],[3],[4]]
# -

# ### Fit label model

final_model_kwargs = dict(lr=1e-3,
                          n_epochs=100)

# +
set_seed(243)

lm = LabelModel(n_epochs=200,
                lr=1e-1)

# Fit and predict on train set
Y_probs = lm.fit(label_matrix=label_matrix,
                 cliques=cliques,
                 class_balance=class_balance).predict()

# Predict on test set
Y_probs_test = lm.predict(label_matrix_test, lm.mu, class_balance[1])

# Analyze test set performance
lm.analyze(y_test, Y_probs_test)
# -

# ### Fit discriminative model

# +
batch_size = 20

set_seed(243)

indices_shuffle = np.random.permutation(len(label_matrix))
split_nr = int(np.ceil(0.9*len(label_matrix)))
train_idx, val_idx = indices_shuffle[:split_nr], indices_shuffle[split_nr:]

train_dataset = CustomTensorDataset(feature_tensor_train, lm.predict_true(y_train).detach())
test_dataset = CustomTensorDataset(feature_tensor_test, torch.Tensor(y_test))

dl_train = DataLoader(CustomTensorDataset(*train_dataset[train_idx]), shuffle=True, batch_size=batch_size)
dl_val = DataLoader(CustomTensorDataset(*train_dataset[val_idx]), shuffle=True, batch_size=batch_size)

# dl_train = DataLoader(train_dataset, shuffle=True, batch_size=batch_size)

test_dataloader = torch.utils.data.DataLoader(dataset=test_dataset, batch_size=batch_size, shuffle=False)

dm = VisualRelationClassifier(pretrained_model, n_epochs=40, lr=1e-3, data_path_prefix="../data/")

dm.reset()
train_preds = dm.fit(dl_train, dl_val).predict()
test_preds = dm.predict(test_dataloader)
print(dm.analyze(y_test, test_preds))
# -

plt.plot(dm.average_train_losses)

plt.plot(dm.average_val_losses)

# +
# plot_train_loss(dm.average_losses)

# +
it = 1
# Choose strategy from ["maxkl", "margin", "nashaat"]
query_strategy = "discriminative"

seed = 48

al = ActiveWeaSuLPipeline(it=it,
                          final_model = VisualRelationClassifier(pretrained_model, **final_model_kwargs, data_path_prefix="../data/"),
                          n_epochs=200,
                          query_strategy=query_strategy,
                          discr_model_frequency=1,
                          penalty_strength=1,
                          batch_size=256,
                          randomness=0,
                          seed=seed,
                          starting_seed=233)

Y_probs_al = al.run_active_weasul(label_matrix=label_matrix,
                                  y_train=y_train,
                                  label_matrix_test=label_matrix_test,
                                  y_test=y_test,
                                  cliques=cliques,
                                  class_balance=class_balance,
                                  train_dataset=CustomTensorDataset(feature_tensor_train, torch.Tensor(y_train)),
                                  test_dataset=CustomTensorDataset(feature_tensor_test, torch.Tensor(y_test)))

# +
# plot_it=5
# plot_probs(df, al.probs["Generative_train"][plot_it], soft_labels=False, add_labeled_points=al.queried[:plot_it])
# -

starting_seed = 36
penalty_strength = 1
nr_trials = 10
al_it = 200

# ### Figure 1

# #### Active WeaSuL

exp_kwargs = dict(nr_trials=nr_trials,
                  al_it=al_it,
                  label_matrix=label_matrix,
                  y_train=y_train,
                  cliques=cliques,
                  class_balance=class_balance,
                  starting_seed=starting_seed,
                  penalty_strength=penalty_strength,
                  batch_size=batch_size,
                  final_model=VisualRelationClassifier(pretrained_model, **final_model_kwargs, data_path_prefix="../data/", early_stopping=True),
                  discr_model_frequency=1,
                  train_dataset = CustomTensorDataset(feature_tensor_train, torch.Tensor(y_train)),
                  test_dataset = CustomTensorDataset(feature_tensor_test, torch.Tensor(y_test)),
                  label_matrix_test=label_matrix_test,
                  y_test=y_test)

np.random.seed(50)
exp_kwargs["seeds"]= np.random.randint(0,1000,nr_trials)
metrics_maxkl, queried_maxkl, probs_maxkl, entropies_maxkl = active_weasul_experiment(**exp_kwargs, query_strategy="maxkl")

exp_kwargs["final_model"] = VisualRelationClassifier(pretrained_model, **final_model_kwargs, data_path_prefix="../data/", early_stopping=True, warm_start=True)
np.random.seed(50)
exp_kwargs["seeds"]= np.random.randint(0,1000,nr_trials)
metrics_maxkl_4, queried_maxkl_4, probs_maxkl_4, entropies_maxkl_4 = active_weasul_experiment(**exp_kwargs, query_strategy="maxkl")
exp_kwargs["final_model"]=VisualRelationClassifier(pretrained_model, **final_model_kwargs, data_path_prefix="../data/", early_stopping=True, warm_start=True)

metrics_maxkl_4

exp_kwargs["final_model"] = VisualRelationClassifier(pretrained_model, **final_model_kwargs, data_path_prefix="../data/", early_stopping=True, warm_start=False)
np.random.seed(50)
exp_kwargs["seeds"]= np.random.randint(0,1000,nr_trials)
metrics_maxkl_3, queried_maxkl_3, probs_maxkl_3, entropies_maxkl_3 = active_weasul_experiment(**exp_kwargs, query_strategy="maxkl")
exp_kwargs["final_model"]=VisualRelationClassifier(pretrained_model, **final_model_kwargs, data_path_prefix="../data/", early_stopping=True, warm_start=True)

# #### Nashaat et al.

np.random.seed(25)
exp_kwargs["seeds"]= np.random.randint(0,1000,nr_trials)
exp_kwargs["final_model"]=VisualRelationClassifier(pretrained_model, **final_model_kwargs, data_path_prefix="../data/", early_stopping=True, warm_start=False)
metrics_nashaat, queried_nashaat, probs_nashaat, _ = active_weasul_experiment(**exp_kwargs, query_strategy="nashaat", randomness=0)
exp_kwargs["final_model"]=VisualRelationClassifier(pretrained_model, **final_model_kwargs, data_path_prefix="../data/", early_stopping=True, warm_start=True)

# #### Active learning

# +
set_seed(76)

train_dataset = CustomTensorDataset(feature_tensor_train[0,:], torch.Tensor(y_train[0]))
predict_dataset = CustomTensorDataset(feature_tensor_train, torch.Tensor(y_train))
test_dataset = CustomTensorDataset(feature_tensor_test, torch.Tensor(y_test))

final_model_kwargs["n_epochs"] = 2

al_exp_kwargs = dict(
    nr_trials=nr_trials,
    al_it=al_it,
    batch_size=batch_size,
    seeds = np.random.randint(0,1000,nr_trials),
    model = VisualRelationClassifier(pretrained_model, **final_model_kwargs, data_path_prefix="../data/", soft_labels=False),
    features = feature_tensor_train,
    y_train = y_train,
    y_test = y_test,
    train_dataset = train_dataset,
    predict_dataloader = DataLoader(dataset=predict_dataset, batch_size=batch_size, shuffle=False),
    test_dataloader = DataLoader(dataset=test_dataset, batch_size=batch_size, shuffle=False),
    test_features=feature_tensor_test
)

final_model_kwargs["n_epochs"] = 100
# -

metrics_activelearning = active_learning_experiment(**al_exp_kwargs)

plot_metrics(process_exp_dict(metrics_activelearning, "Active learning"), ["F1"])

# #### Process results

metric_dfs = pd.concat([process_exp_dict(metrics_maxkl, "Active WeaSuL"),
                        process_exp_dict(metrics_maxkl_3, "Active WeaSuL 2"),
                        process_exp_dict(metrics_nashaat, "Nashaat et al."),
                        process_exp_dict(metrics_activelearning, "Active learning by itself")])
metric_dfs = metric_dfs.reset_index(level=0).rename(columns={"level_0": "Run"})
metric_dfs["Dash"] = "n"
mean_f1 = joined_df[(joined_df["Model"] == "Discriminative") & joined_df["Approach"].isin(["Active learning by itself"])].drop(["Run"], axis=1).groupby(["Approach", "Number of labeled points"]).mean().reset_index(level=1)


mean_f1[50:70]

mean_f1[mean_f1["Value"] > 0.6]

# +
joined_df = add_baseline(metric_dfs, al_it)

# optimal_generative_test = lm.analyze(y_test, lm.predict_true(y_train, y_test, label_matrix_test))
# optimal_discriminative_test = dm.analyze(y_test, test_preds)
# joined_df = add_optimal(joined_df, al_it, optimal_generative_test, optimal_discriminative_test)


# +
# joined_df.to_csv("../results/figure_4B.csv", index=False)
# -

joined_df = joined_df[joined_df["Metric"] == "F1"]
joined_df = joined_df[joined_df["Set"] == "test"]


font_size=25
legend_size=25
tick_size=20
n_boot=10000
linewidth=4

# +
# joined_df.to_csv("../results/figure_4B.csv", index=False)

# +
colors = ["#2b4162", "#368f8b", "#ec7357", "#e9c46a", "#ffaaef"]

sns.set(style="whitegrid", palette=sns.color_palette(colors))

fig, axes = plt.subplots(1,1, figsize=(15,8), sharey=True)

plt.tight_layout()

# sns.lineplot(data=joined_df[joined_df["Model"] == "Generative"], x="Number of labeled points", y="Value",
#             hue="Approach", ci=68, n_boot=n_boot, estimator="mean", legend=False, linewidth=linewidth,
#             hue_order=["Active WeaSuL", "Nashaat et al.", "Weak supervision by itself"], ax=axes[0])

# axes[0].set_title("Generative model (50 iterations)", size=font_size)

sns.lineplot(data=joined_df[joined_df["Model"] == "Discriminative"], x="Number of labeled points", y="Value",
            hue="Approach", ci=68, n_boot=n_boot, estimator="mean", linewidth=linewidth,
            hue_order=["Active WeaSuL", "Nashaat et al.", "Weak supervision by itself", "Active learning by itself"])
axes.set_title("Discriminative model", fontsize=font_size)

handles, labels = axes.get_legend_handles_labels()
[ha.set_linewidth(linewidth) for ha in handles]
leg = axes.legend(handles=handles[0:], labels=labels[0:6], loc="lower right", title="Method",
                     fontsize=legend_size, title_fontsize=legend_size)
leg._legend_box.align = "left"
# leg_lines = leg.get_lines()
# leg_lines[0].set_linestyle("--")

axes.tick_params(axis='both', which='major', labelsize=tick_size)
# axes[1].tick_params(axis='both', which='major', labelsize=tick_size)

axes.set_xlabel("Number of active learning iterations", fontsize=font_size)
# axes[1].set_xlabel("Number of active learning iterations", fontsize=font_size)
axes.set_ylabel("F1", fontsize=font_size)

# plt.ylim(-0.2,0.7)

plt.tight_layout()

plt.savefig("../plots/VRD_performance_baselines_13.png")
plt.show()
# -

# ## Figure 2AB

# #### Other sampling strategies

exp_kwargs["al_it"] = 50

np.random.seed(40)
exp_kwargs["seeds"]= np.random.randint(0,1000,nr_trials)
metrics_margin, queried_margin, probs_margin, entropies_margin = active_weasul_experiment(**exp_kwargs, query_strategy="margin")

exp_kwargs["final_model"] = VisualRelationClassifier(pretrained_model, **final_model_kwargs, data_path_prefix="../data/", early_stopping=True, warm_start=False)
np.random.seed(70)
exp_kwargs["seeds"]= np.random.randint(0,1000,nr_trials)
metrics_margin_2, queried_margin_2, probs_margin_2, entropies_margin_2 = active_weasul_experiment(**exp_kwargs, query_strategy="margin")
exp_kwargs["final_model"]=VisualRelationClassifier(pretrained_model, **final_model_kwargs, data_path_prefix="../data/", early_stopping=True, warm_start=True)

np.random.seed(60)
exp_kwargs["seeds"]= np.random.randint(0,1000,10)
metrics_discr, queried_discr, probs_discr, entropies_discr = active_weasul_experiment(**exp_kwargs, query_strategy="discriminative")

np.random.seed(70)
exp_kwargs["seeds"]= np.random.randint(0,1000,nr_trials)
metrics_random, queried_random, probs_random, entropies_random = active_weasul_experiment(**exp_kwargs, query_strategy="margin", randomness=1)

# #### Process results

metric_dfs = pd.concat([process_exp_dict(metrics_maxkl, "MaxKL"),
                        process_exp_dict(metrics_margin, "Margin"),
                        process_exp_dict(metrics_random, "Random")])

# +
metric_dfs = metric_dfs[metric_dfs.Set == "test"]
metric_dfs = metric_dfs[metric_dfs["Metric"].isin(["F1"])]
metric_dfs = metric_dfs[metric_dfs["Number of labeled points"] < 51]

lines = list(metric_dfs.Approach.unique())

colors = ["#2b4162", "#368f8b", "#ec7357", "#e9c46a"][:len(lines)]

sns.set(style="whitegrid", palette=sns.color_palette(colors))

fig, axes = plt.subplots(1,2, figsize=(15,8), sharey=True)

sns.lineplot(data=metric_dfs[metric_dfs["Model"] == "Generative"], x="Number of labeled points", y="Value",
            hue="Approach", ci=68, n_boot=n_boot, estimator="mean", linewidth=linewidth,
            hue_order=["MaxKL",  "Margin", "Random"], ax=axes[0])

handles, labels = axes[0].get_legend_handles_labels()
[ha.set_linewidth(linewidth) for ha in handles]
axes[0].legend(handles=handles, labels=labels, loc="lower right", title="Sampling method", fontsize=legend_size, title_fontsize=legend_size)
axes[0].set_title("Generative model", fontsize=font_size)

sns.lineplot(data=metric_dfs[metric_dfs["Model"] == "Discriminative"], x="Number of labeled points", y="Value",
            hue="Approach", ci=68, n_boot=n_boot, estimator="mean", linewidth=linewidth, legend=False,
            hue_order=["MaxKL", "Margin", "Random"], ax=axes[1])

axes[1].set_title("Discriminative model", fontsize=font_size)

axes[0].tick_params(axis='both', which='major', labelsize=tick_size)
axes[1].tick_params(axis='both', which='major', labelsize=tick_size)

axes[0].set_xlabel("Number of active learning iterations", fontsize=font_size)
axes[1].set_xlabel("Number of active learning iterations", fontsize=font_size)
axes[0].set_ylabel("F1", fontsize=font_size)

# plt.ylim(0.5,1)

plt.tight_layout()

plt.savefig("../plots/VRD_sampling_strategies_11.png")
plt.show()
# -

# ### Figure 2C

# #### Process entropies

# +
maxkl_entropies_df = pd.DataFrame.from_dict(entropies_maxkl).stack().reset_index().rename(columns={"level_0": "Number of labeled points", "level_1": "Run", 0: "Entropy"})
maxkl_entropies_df["Approach"] = "MaxKL"

margin_gen_entropies_df = pd.DataFrame.from_dict(entropies_margin).stack().reset_index().rename(columns={"level_0": "Number of labeled points", "level_1": "Run", 0: "Entropy"})
margin_gen_entropies_df["Approach"] = "Margin"

margin_discr_entropies_df = pd.DataFrame.from_dict(entropies_discr).stack().reset_index().rename(columns={"level_0": "Number of labeled points", "level_1": "Run", 0: "Entropy"})
margin_discr_entropies_df["Approach"] = "Margin-discriminative"

random_entropies_df = pd.DataFrame.from_dict(entropies_random).stack().reset_index().rename(columns={"level_0": "Number of labeled points", "level_1": "Run", 0: "Entropy"})
random_entropies_df["Approach"] = "Random"
# -

entropies_df = pd.concat([maxkl_entropies_df, margin_gen_entropies_df, random_entropies_df])
entropies_df["Number of labeled points"] = entropies_df["Number of labeled points"].apply(lambda x: x+1)
entropies_df = entropies_df[entropies_df["Number of labeled points"] < 51]

# +
# colors = ["#ec7357", "#2b4162", "#3,68f8b", "#e9c46a"]
lines = list(entropies_df.Approach.unique())

colors = ["#2b4162", "#368f8b", "#ec7357", "#e9c46a"][:len(lines)]

sns.set(style="whitegrid", palette=sns.color_palette(colors))

plt.subplots(1,1,figsize=(8,8))
ax = sns.lineplot(data=entropies_df, x="Number of labeled points", y="Entropy", hue="Approach", ci=68, n_boot=n_boot,
                  legend=False,linewidth=linewidth, hue_order=["MaxKL", "Margin","Random"])
ax.tick_params(axis='both', which='major', labelsize=tick_size)

ax.set_xlabel("Number of active learning iterations", fontsize=font_size)
ax.set_ylabel("Diversity (entropy)", fontsize=font_size)
ax.set_title("Diversity of sampled buckets", fontsize=font_size)
# plt.ylim(-0.05,1.8)

plt.tight_layout()
plt.savefig("../plots/VRD_entropies_6.png")
# plt.show()
# -






