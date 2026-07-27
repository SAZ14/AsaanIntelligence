# Loans Agent (demo)

Scans the bank's customer book, grades every relationship, detects genuine
cash stress, and decides **who gets a proactive instant-loan offer, who is
monitored, and who is declined** — with secured alternatives so a declined
customer still leaves with a workable path.

## Design: the policy decides, the model narrates

```
customer_book.json ──► policy.py (deterministic)         llm.py (local model)
                        │ relationship score + band        │
                        │ cash-stress detection            │
                        │ EMI / DBR / affordability        │
                        │ DECISION GATE                    │
                        ▼                                  ▼
                     Decision ──────────────────────► narrative / free-form Q&A
```

Every number and every OFFER/MONITOR/DECLINE decision is computed in
`app/agents/loans/policy.py`. The LLM receives the policy verdict as ground
truth and only explains it — a hallucinating model can never approve a file
the policy declines.

### The policy (mirrors the fine-tuning dataset)

**Relationship score** — account age, salary regularity, average balance, and
repayment history earn up to +8; a cheque bounce (−3), eCIB 90+ DPD (−4), or
write-off/litigation flag (−6) pull it down.
Bands: `>=6 STRONG | 3-5 ACCEPTABLE | 0-2 THIN | <0 POOR`.

**Cash stress** — 10+ days under Rs 5,000 in the last 30 is the genuine-stress
marker (plus balance-trajectory context).

**Decision gate**
- **OFFER**: STRONG/ACCEPTABLE **and** genuinely stressed — sized under the
  40% DBR cap, floored to Rs 10,000, clamped to the product band.
- **MONITOR**: THIN files; liquid STRONG/ACCEPTABLE files (keep pre-approved);
  stressed files with no instalment headroom or income under the product floor.
- **DECLINE**: POOR files get no unsecured credit — stress makes unsecured
  lending to a bad file *more* dangerous, not less. They are redirected to
  deposit-backed / gold-backed finance, a secured card to rebuild eCIB, and
  the eCIB correction process where the flag is disputed.

## Run the demo

```bash
python scripts/loan_demo.py                  # full-book triage queues
python scripts/loan_demo.py --walkthrough    # + narrated deep-dives
python scripts/loan_demo.py --customer C007  # one file in detail
python scripts/loan_demo.py --no-llm         # fully offline (template narratives)
```

API (mounted on the central server):

```
GET  /loans/book                        book summary with scores/bands
GET  /loans/customers/{cid}             profile + score breakdown + stress signals
GET  /loans/customers/{cid}/decision    policy decision (?narrative=true → LLM)
GET  /loans/scan                        OFFER / MONITOR / DECLINE queues
POST /loans/ask                         free-form credit question {question, customer_id?}
```

## Local model

Works out of the box against **Ollama** (`llama3.1:8b` by default) with
few-shot examples from `data/loans/fewshot.jsonl`; degrades gracefully to
deterministic template narratives when no model is running.

```
LOAN_LLM_BASE_URL   default http://localhost:11434/v1
LOAN_LLM_MODEL      default llama3.1:8b
LOAN_FEWSHOT        default 4 (set 0 after fine-tuning)
```

## Fine-tuning

`data/loans/loans_pakistan_finetune_10k.jsonl` — 10k examples across 17
credit-ops task types, half English half Roman Urdu. Train with
`notebooks/finetune_loans_qlora_colab.ipynb` (Unsloth QLoRA, free Colab T4,
~1–2 h), export GGUF, `ollama create loans-agent`, then:

```bash
export LOAN_LLM_MODEL=loans-agent LOAN_FEWSHOT=0
```

The agent's prompts (`app/agents/loans/prompts.py`) byte-match the dataset's
format, so the fine-tuned model is a drop-in.

## Synthetic customer book

`scripts/generate_loan_book.py` generates a seeded, reproducible 60-customer
book covering every policy branch (strong+stressed, liquid, thin, poor).
All customers, banks' pairings, and balances are synthetic.
