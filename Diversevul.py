import re
import os
import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification, Trainer, TrainingArguments, AutoModel
from datasets import Dataset, load_dataset
import pandas as pd
import numpy as np
import random
from sklearn.model_selection import train_test_split
import pandas as pd
from datasets import Dataset, DatasetDict
import torch.nn as nn
import torch
import matplotlib.pyplot as plt
from sklearn.metrics import confusion_matrix
import seaborn as sns

seed = 42
random.seed(seed)
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)
np.random.seed(seed)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

device = 'cuda' if torch.cuda.is_available() else 'cpu'
print("Using device: ", device)
model_ckpt_c = 'neulab/codebert-c'
model_ckpt_cpp = 'neulab/codebert-cpp'
model_ckpt_t5 = 'Salesforce/codet5p-110m-embedding'
model_ckpt_unixcoder = 'microsoft/unixcoder-base'
model_codesage_small = 'codesage/codesage-small'
model_roberta = 'FacebookAI/roberta-base'
model_name = model_ckpt_t5
tokenizer = AutoTokenizer.from_pretrained(model_name)
file_path = 'Diversevul.csv'
data = pd.read_csv(file_path)
data = data.rename(columns={'func': 'code', 'target': 'label'})
print(len(data))
data = data[['code', 'label']]
data['label'].value_counts()
print(data['label'].value_counts())
comment_regex = r'(//[^\n]*|\/\*[\s\S]*?\*\/)'
newline_regex = '\n{1,}'
whitespace_regex = '\s{2,}'

def data_cleaning(inp, pat, rep):
    return re.sub(pat, rep, inp)

data['truncated_code'] = (data['code'].apply(data_cleaning, args=(comment_regex, ''))
                                      .apply(data_cleaning, args=(newline_regex, ' '))
                                      .apply(data_cleaning, args=(whitespace_regex, ' '))
                         )
length_check = np.array([len(x) for x in data['truncated_code']]) > 15000
data = data[~length_check]
X_train, X_test, y_train, y_test = train_test_split(data.loc[:, data.columns != 'label'],
                                                                data['label'],
                                                                train_size=0.8,
                                                                stratify=data['label']
                                                               )
data_train = X_train
data_train['label'] = y_train
data_test = X_test
data_test['label'] = y_test

dts = DatasetDict()
dts['train'] = Dataset.from_pandas(data_train)
dts['test'] = Dataset.from_pandas(data_test)

def tokenizer_func(examples):
    result = tokenizer(examples['truncated_code'], max_length=512, padding='max_length', truncation=True)
    return result

dts = dts.map(tokenizer_func,
             batched=True,
             batch_size=4
             )

dts.set_format('torch')
dts.rename_column('label', 'labels')
dts = dts.remove_columns(['code', 'truncated_code', '__index_level_0__'])
import torch.nn as nn
import torch
from transformers import AutoModel

class PositionalEncoding(nn.Module):
    def __init__(self, d_model, max_len, dropout=0.1, padding_idx=0):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)
        self.pos_encoding = nn.Embedding(max_len, d_model, padding_idx=padding_idx)

    def forward(self, x):
        device = x.device
        chunk_size, B, d_model = x.shape
        position_ids = torch.arange(0, chunk_size, dtype=torch.int).unsqueeze(1).to(device)
        position_enc = self.pos_encoding(position_ids).expand(chunk_size, B, d_model)
        x = x + position_enc
        x = self.dropout(x)
        return x

class CodeBertModel(nn.Module):
    def __init__(self,
                 max_seq_length: int = 512,
                 chunk_size: int = 512,
                 padding_idx: int = 0,
                 model_ckpt: str = '',
                 num_heads: int = 8,
                 **from_pretrained_kwargs):
        super().__init__()
        self.embedding_model = AutoModel.from_pretrained(model_ckpt, trust_remote_code=True).to(device)

        dict_config = self.embedding_model.config.to_dict()
        for sym in ['hidden_dim', 'embed_dim', 'hidden_size']:
            if sym in dict_config.keys():
                embed_dim = dict_config[sym]

        encoder_layer = nn.TransformerEncoderLayer(d_model=embed_dim,
                                                   nhead=num_heads,
                                                   dim_feedforward=768,
                                                   batch_first=False)

        self.transformer_encoder = nn.TransformerEncoder(encoder_layer=encoder_layer,
                                                         num_layers=12,
                                                         )

        self.positional_encoding = PositionalEncoding(max_len=max_seq_length,
                                                      d_model=embed_dim,
                                                      padding_idx=padding_idx)

        self.loss_func = nn.CrossEntropyLoss(weight=torch.Tensor([1.0, 6.0]),
                                             label_smoothing=0.2)

        self.ffn = nn.Sequential(nn.Dropout(p=0.1),
                                 nn.Linear(embed_dim, 2)
                                 )
        self.chunk_size = chunk_size

    def prepare_chunk(self, input_ids: torch.Tensor,
                            attention_mask: torch.Tensor,
                            labels=None):
        """
        Prepare inputs into chunks that self.embedding_model can process (length < context_length)
        Shape info:
        - input_ids: (B, L)
        - attention_mask: (B, L)
        """

        # calculate number of chunks
        num_chunk = input_ids.shape[-1] // self.chunk_size
        if input_ids.shape[-1] % self.chunk_size != 0:
            num_chunk += 1
            pad_len = self.chunk_size - (input_ids.shape[-1] % self.chunk_size)
        else:
            pad_len = 0

        B = input_ids.shape[0]
        # get the model's pad_token_id
        pad_token_id = self.embedding_model.config.pad_token_id

        # create a pad & zero tensor, then append it to the input_ids & attention_mask tensor respectively
        pad_tensor = torch.Tensor([pad_token_id]).expand(input_ids.shape[0], pad_len).int().to(device)
        zero_tensor = torch.zeros(input_ids.shape[0], pad_len).int().to(device)
        padded_input_ids = torch.cat([input_ids, pad_tensor], dim = -1).T # (chunk_size * num_chunk, B)
        padded_attention_mask = torch.cat([attention_mask, zero_tensor], dim = -1).T # (chunk_size * num_chunk, B)

        chunked_input_ids = padded_input_ids.reshape(num_chunk, self.chunk_size, B).permute(0, 2, 1) # (num_chunk, B, chunk_size)
        chunked_attention_mask = padded_attention_mask.reshape(num_chunk, self.chunk_size, B).permute(0, 2, 1) # (num_chunk, B, chunk_size)

        pad_chunk_mask = self.create_chunk_key_padding_mask(chunked_input_ids)

        return chunked_input_ids, chunked_attention_mask, pad_chunk_mask

    def create_chunk_key_padding_mask(self, chunks):
        """
        If a chunk contains only pad tokens, ignore that chunk
        chunks: B, num_chunk, chunk_size
        """
        pad_token_id = self.embedding_model.config.pad_token_id
        pad_mask = (chunks == pad_token_id)

        num_pad = (torch.sum(pad_mask, -1) == self.chunk_size).permute(1, 0) # (num_chunk, B)

        return num_pad

    def forward(self, input_ids, attention_mask, labels=None):

        # calculate numbers of chunk
        chunked_input_ids, chunked_attention_mask, pad_chunk_mask = self.prepare_chunk(input_ids, attention_mask) # (num_chunk, B, chunk_size), (num_chunk, B, chunk_size), (num_chunk, B)

        # reshape input_ids & attention_mask tensors to fit into embedding model
        num_chunk, B, chunk_size = chunked_input_ids.shape
        chunked_input_ids, chunked_attention_mask = chunked_input_ids.contiguous().view(-1, chunk_size), chunked_attention_mask.contiguous().view(-1, self.chunk_size) # (B * num_chunk, chunk_size), (B * num_chunk, chunk_size)

        # embedded_chunks = (self.embedding_model(input_ids = chunked_input_ids,
        #                                         attention_mask = chunked_attention_mask)['pooler_output'] # (B * num_chunk, self.embedding_model.config.hidden_dim)
        #                        .view(num_chunk, B, -1) # (num_chunk, B, self.embedding_model.config.hidden_dim)
        #                   )

        embedded_chunks = (self.embedding_model(input_ids = chunked_input_ids,
                                                attention_mask = chunked_attention_mask) # (B * num_chunk, self.embedding_model.config.hidden_dim)
                               .view(num_chunk, B, -1) # (num_chunk, B, self.embedding_model.config.hidden_dim)
                          )

        embedded_chunks = self.positional_encoding(embedded_chunks)

        output = self.transformer_encoder(embedded_chunks,
                                          src_key_padding_mask = pad_chunk_mask) # (num_chunk, B, self.embedding_model.config.hidden_dim)

        logits = self.ffn(output[0])

        if labels is not None:
            loss = self.loss_func(logits, labels)
            return {"loss": loss, "logits": logits}

        return {"logits": logits}

from sklearn.metrics import precision_score, accuracy_score, recall_score, f1_score
def compute_metrics(eval_pred):
    y_pred, y_true = np.argmax(eval_pred.predictions, -1), eval_pred.label_ids
    return {'accuracy': accuracy_score(y_true, y_pred),
            'precision': precision_score(y_true, y_pred),
            'recall': recall_score(y_true, y_pred),
            'f1': f1_score(y_true, y_pred)}

model = CodeBertModel(model_ckpt = model_name, max_seq_length=512, chunk_size = 512, num_heads=4).to(device)

from transformers import DataCollatorWithPadding
import os
data_collator = DataCollatorWithPadding(tokenizer=tokenizer)

directory = "modelsave"
if not os.path.exists(directory):
    os.makedirs(directory)

training_arguments = TrainingArguments(output_dir = './modelsave',
                                      evaluation_strategy = 'epoch',
                                      per_device_train_batch_size = 5,
                                      per_device_eval_batch_size = 5,
                                      gradient_accumulation_steps = 12,
                                      learning_rate = 3e-5,
                                      num_train_epochs = 50,
                                      warmup_ratio = 0.1,
                                      lr_scheduler_type = 'cosine',
                                      logging_strategy = 'steps',
                                      logging_steps = 10,
                                      save_strategy = 'no',
                                      fp16 = True,
                                      metric_for_best_model = 'accuracy',
                                      optim = 'adamw_torch',
                                      report_to = 'none',
                                      )
trainer = Trainer(model=model,
                  data_collator=data_collator,
                  args=training_arguments,
                  train_dataset=dts['train'],
                  eval_dataset=dts['valid'],
                  compute_metrics=compute_metrics,
                 )
trainer.train()

check = trainer.predict(dts['test'])

print(compute_metrics(check))

def plot_confusion(eval_pred):
    y_pred, y_true = np.argmax(eval_pred.predictions, -1), eval_pred.label_ids
    cm = confusion_matrix(y_true, y_pred)

# Plot confusion matrix
    plt.figure(figsize=(8, 6))
    sns.heatmap(cm, annot=True, fmt='d', cmap='Blues', xticklabels=['Negative', 'Positive'], yticklabels=['Negative', 'Positive'])
    plt.xlabel('Predicted Labels')
    plt.ylabel('Actual Labels')
    plt.title('Confusion Matrix')
    plt.show()


plot_confusion(check)
