import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

# 1. Load the model and tokenizer
model_id = "EliasAhl/llama-3-8b-Instruct-cefr-tuned-v2"
tokenizer = AutoTokenizer.from_pretrained(model_id)
model = AutoModelForCausalLM.from_pretrained(model_id)

# 2. Define the German system prompt
# This explicitly instructs the model on its role, the CEFR scale, and what to output.
system_prompt = (
    "Du bist ein Sprachexperte. Deine Aufgabe ist es, den folgenden Text nach dem "
    "Gemeinsamen Europäischen Referenzrahmen für Sprachen (CEFR) zu bewerten. "
    "Die Stufen sind: A1, A2, B1, B2, C1 und C2, wobei A1 für absolute Anfänger "
    "und C2 für muttersprachliches Niveau steht. Analysiere den Text und antworte "
    "mit dem passenden CEFR-Level, gefolgt von einer kurzen Begründung."
)


# 3. Create a helper function to process any text
def grade_text(text_content):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Bitte bewerte den folgenden Text:\n\n{text_content}"}
    ]

    input_ids = tokenizer.apply_chat_template(
        messages,
        add_generation_prompt=True,
        return_tensors="pt"
    ).to(model.device)

    # Generate the prediction (using do_sample=False for a deterministic answer)
    outputs = model.generate(
        input_ids,
        max_new_tokens=150,
        do_sample=False,
        pad_token_id=tokenizer.eos_token_id
    )

    input_length = input_ids.shape[1]
    response = tokenizer.decode(outputs[0][input_length:], skip_special_tokens=True)
    return response


# 4. Loop through your text files and print the results
files_to_evaluate = ["easy.txt", "hard.txt"]

for filename in files_to_evaluate:
    try:
        # Read the contents of the file
        with open(filename, "r", encoding="utf-8") as file:
            text = file.read()

        print(f"=== Grading: {filename} ===")

        # Call the model
        result = grade_text(text)
        print(result)
        print("\n" + "=" * 40 + "\n")

    except FileNotFoundError:
        print(f"⚠️ Error: Could not find '{filename}'. Please make sure it is in the same folder as this script.\n")