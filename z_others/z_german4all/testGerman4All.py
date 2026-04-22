from peft import PeftModel
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

model_name = "tum-nlp/single_model_xl"
base_model = AutoModelForSeq2SeqLM.from_pretrained("google/flan-t5-xl")
model = PeftModel.from_pretrained(base_model, model_name).to(base_model.device)
tokenizer = AutoTokenizer.from_pretrained(model_name)

description = """Verwandle den folgenden Text in die Zielsprachebene gemäß den untenstehenden Beschreibungen.
KOMPLEXITÄTSBESCHREIBUNGEN
Level 1 : Leichte Sprache:
Zielgruppe: Zielgruppe: Personen mit Leseschwierigkeiten, inklusive Menschen mit Lernbehinderungen und solche, die erst kürzlich begonnen haben, Deutsch zu lernen.
Merkmale: Sehr kurze Sätze, nur kurze und häufig verwendete Wörter, direkte Ansprache. Vermeidung von Abkürzungen, Metaphern oder Ironie.
Beispiele: Einfache Anleitungen, barrierefreie Webseiten.

Level 2 : Einfaches Deutsch für Anfänger:
Zielgruppe: Nicht-Muttersprachler mit grundlegenden Deutschkenntnissen.
Merkmale: Einfache Satzstrukturen, Grundwortschatz, klarer Fokus auf wichtige Informationen, Vermeidung kulturspezifischer Ausdrücke.
Beispiele:  Sprachlernmaterialien, einführende Webtexte.

Level 3 : Gebräuchliche Standardsprache:
Zielgruppe: Öffentlichkeit mit unterschiedlichem Bildungsniveau.
Merkmale: Klare, strukturierte Sätze, Verständlichkeit steht im Vordergrund, Vermeidung von Fachjargon.
Beispiele: Breit gefächerte Nachrichtenportale, Blogs.

Level 4 : Gehobene Alltagssprache:
Zielgruppe: Regelmäßige Leser mit gutem Sprachverständnis.
Merkmale: Vielfältigeres Vokabular, gelegentlicher Fachjargon mit Erklärungen, komplexe Satzstrukturen.
Beispiele: Fachblogs, Qualitätszeitungen.

Level 5 : Akademische Sprache:
Zielgruppe: Akademiker und Experten.
Merkmale: Komplexe Satzstrukturen, Fachterminologie, Verwendung von Fachbegriffen.
Beispiele: Fachzeitschriften, wissenschaftliche Publikationen.
"""

input_text = input()
# TODO: Define your target level
level = 2
prefix = f"\n Paraphrasiere den folgenden Text auf Level {level}.\n Text: "
input_full = [description + prefix.format(level=level) + input_text]
model_inputs = tokenizer(input_full, max_length=512, truncation=True, padding="max_length",
                         return_tensors="pt").to(model.device)
eval_preds = model.generate(**model_inputs, do_sample=True, top_p=0.9, temperature=0.1,
                          max_new_tokens=256)
output_preds = tokenizer.batch_decode(eval_preds, skip_special_tokens=True)[0]
print(f"Output to level {level}:", output_preds)

# prints "Output to level 1: In Deutschland gibt es einen großen See. Der Bodensee ist sehr groß. Viele Menschen gehen dort zum Baden."
