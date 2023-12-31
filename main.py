import torch
import transformers
from datasets import Dataset
import pandas as pd
from scipy.stats import entropy
from collections import defaultdict
from trl import DPOTrainer
import os

MODEL_TYPE = 'hinge'  # hinge or sigmoid
START_TYPE = 'train'  # inference or train

#Creating dataset from generated data using dataset.Dataset

def prepare_dataset(prompts, texts, logits):
    """
    len(texts) == len(logits) == len(prompts)*2
    """
    win_los = []
    for i in range(0, len(logits), 2):
        if logits[i][1] > logits[i + 1][1]:
            win_los.append([(texts[i], 'chosen'), (texts[i + 1], 'rejected')])
        else:
            win_los.append([(texts[i], 'rejected'), (texts[i + 1], 'chosen')])

    dpo = {'prompt': [], 'chosen': [], 'rejected': []}
    for i in range(50):
        dpo['prompt'].append(prompts[i] + ' review:')
        dpo[win_los[i][0][1]].append(win_los[i][0][0])
        dpo[win_los[i][1][1]].append(win_los[i][1][0])

    return Dataset.from_dict(dpo)



def token_entropy(generations, tokenizer):
    stats = defaultdict(int)
    num_tokens = 0
    for example in generations:
        tokens = tokenizer.encode(example)
        for t in tokens:
            if t == tokenizer.pad_token_id:
                continue
            stats[t] += 1
            num_tokens += 1
    for k in stats.keys():
        stats[k] /= num_tokens
    return entropy(list(stats.values()))


def main():
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    # device = 'cpu'
    main_tokenizer = transformers.AutoTokenizer.from_pretrained("lvwerra/gpt2-imdb", device_map=device)
    main_model = transformers.AutoModelForCausalLM.from_pretrained("lvwerra/gpt2-imdb", device_map=device)
    if main_tokenizer.pad_token is None:
        main_tokenizer.pad_token = main_tokenizer.eos_token

    reward_tokenizer = transformers.AutoTokenizer.from_pretrained("lvwerra/distilbert-imdb")
    reward_model = transformers.AutoModelForSequenceClassification.from_pretrained("lvwerra/distilbert-imdb",
                                                                                   device_map=device)

    ## Generating with titles of top 50 films from IMDB

    df = pd.read_csv('titles.csv', header=None)

    prompts = df[0].apply(lambda x: x + ' review:').tolist()
    input = main_tokenizer(prompts, return_tensors="pt", padding=True).to('cuda')
    output = main_model.generate(**input, max_length=50, no_repeat_ngram_size=2, do_sample=True, top_p=0.4,
                                 num_return_sequences=2)

    generated_texts = []

    for i in range(100):
        generated_text = main_tokenizer.decode(output[i], skip_special_tokens=True)
        generated_texts.append(generated_text)

    input_rewards = reward_tokenizer(generated_texts, return_tensors="pt", padding=True).to("cuda")
    with torch.no_grad():
        logits = reward_model(**input_rewards).logits

    if START_TYPE=='train':
        model = transformers.AutoModelForCausalLM.from_pretrained("lvwerra/gpt2-imdb", device_map=device)
        model_ref = transformers.AutoModelForCausalLM.from_pretrained("lvwerra/gpt2-imdb", device_map=device)
        tokenizer = transformers.AutoTokenizer.from_pretrained("lvwerra/gpt2-imdb")
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        training_args = transformers.TrainingArguments(
            output_dir="./test",
            remove_unused_columns=False)

        dataset = prepare_dataset(prompts, generated_texts, logits)

        dpo_trainer = DPOTrainer(
            model,
            model_ref,
            args=training_args,
            beta=0.1,
            train_dataset=dataset,
            tokenizer=tokenizer,
            loss_type='hinge',
        )

        dpo_trainer.train()

        dpo_trainer.save_model(f"./model_{MODEL_TYPE}")

    new_model = transformers.AutoModelForCausalLM.from_pretrained(f"model_{MODEL_TYPE}", local_files_only=True,
                                                                  device_map=device)

    output = new_model.generate(**input, max_length=50, no_repeat_ngram_size=2, do_sample=True, top_p=0.4,
                                num_return_sequences=2, pad_token_id=main_tokenizer.eos_token_id)

    generated_texts_alligned = []

    for i in range(100):
        generated_text = main_tokenizer.decode(output[i], skip_special_tokens=True)
        generated_texts_alligned.append(generated_text)

    input_rewards = reward_tokenizer(generated_texts_alligned, return_tensors="pt", padding=True).to("cuda")
    with torch.no_grad():
        logits_alligned = reward_model(**input_rewards).logits

    if not os.path.exists('./results'):
        os.makedirs('./results')

    with open('./results/sft_text_and_logits.txt', 'w', encoding='UTF-8') as f:
        f.write('logits for every generated text without allignment:\n')
        for text,logit in zip(generated_texts,logits):
            f.write(text+'\n')
            f.write(f'logits value: {logit.tolist()}\n')
    with open(f'./results/{MODEL_TYPE}_alligned_text_and_logits.txt', 'w', encoding='UTF-8') as f:
        f.write('logits for every generated text with allignment:\n')
        for text, logit in zip(generated_texts_alligned, logits_alligned):
            f.write(text+'\n')
            f.write(f'logits value: {logit.tolist()}\n')
    with open('./results/diversity_results.txt', 'a', encoding='UTF-8') as f:
        f.write(f'basic model entropy diversity:{token_entropy(generated_texts, main_tokenizer)}\n')
        f.write(f'{MODEL_TYPE} alligned model entropy diversity:{token_entropy(generated_texts_alligned, main_tokenizer)}')
    with open('./results/logits_comparison.txt', 'a', encoding='UTF-8') as f:
        f.write(
            f'average logit values of baseline SFT model: {[sum([x[1] for x in logits.tolist()])/len(logits), sum([x[0] for x in logits.tolist()])/len(logits)]}\n')
        f.write(
            f'average logit values of {MODEL_TYPE} alligned model: {[sum([x[0] for x in logits_alligned.tolist()]) / len(logits), sum([x[1] for x in logits_alligned.tolist()]) / len(logits)]}\n')


if __name__ == '__main__':
    main()
