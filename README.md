Adaptive Language Learning Platform for German

A multi-modal, closed-loop cognitive load system that dynamically adapts German
texts to a user's exact proficiency level. The platform integrates reading,
spaced repetition, and real-time chat, utilizing locally hosted Large Language
Models to rewrite complex texts while strictly retaining the specific vocabulary
words the user is currently studying.

Core Features

  - Stories (Reading): Upload raw texts (TXT, PDF, EPUB). The backend
    automatically chunks the text and uses spaCy to extract vocabulary against a
    MongoDB-ingested Kaikki dictionary. Users can read, view definitions, and
    export unknown words to their flashcards.
  - Flashcards (FSRS): Implements the Free Spaced Repetition Scheduler. The
    system tracks explicit memory states (Retrievability, Stability, Difficulty)
    to quantify the user's exact vocabulary size.
  - Real-Time Chat: WebSocket-based peer-to-peer messaging. If a beginner
    receives a complex message from an advanced peer, they can trigger an AI
    simplification that translates the message down to their comprehension
    level.

AI & Machine Learning Pipeline

  - Dynamic Level Estimator: A mathematical engine that calculates a global
    proficiency score (1.0 to 6.0) by blending implicit chat/reading metrics and
    explicit FSRS retention data.
  - Evaluator Model (GeistBERT): Fine-tuned for continuous regression (MSE) to
    score text difficulty. The training pipeline utilizes strict tensor
    truncation and random sentence cropping to completely eliminate length bias.
  - Generator Model (Flan-T5-XL): Fine-tuned via QLoRA. Adapters were applied to
    both attention matrices and dense feed-forward networks to ensure the model
    simplifies grammar while strictly obeying prompts that mandate the retention
    of user-known vocabulary.

Tech Stack

  - Frontend: React, TypeScript, Material-UI, react-resizable-panels
  - Backend: FastAPI, Python, WebSockets, asyncio
  - Database & Caching: MongoDB, Redis
  - NLP & ML: PyTorch, Hugging Face Transformers, PEFT (LoRA), spaCy

Local Development Setup

1.  Prerequisites: Ensure MongoDB and Redis are installed and running locally.
2.  Backend:
      - Navigate to the backend directory.
      - Install dependencies: pip install -r requirements.txt
      - Start the server: uvicorn main:app --reload
3.  Frontend:
      - Navigate to the frontend directory.
      - Install dependencies: npm install
      - Start the development server: npm run dev
4.  AI Models:
      - The system requires local weights for the fine-tuned GeistBERT and
        Flan-T5-XL models. Update the model directory paths in the backend
        configuration before running inference.
