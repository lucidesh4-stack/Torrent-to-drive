# 🛡️ ZRP_STRICT: Zero-Regression Protocol

> This is an IMPERATIVE Operational Standard. Failure to follow this is a protocol violation.

## 🚩 The Golden Rule
**Never assume the code works. Never assume the user is correct. Aggressively search for failure.**

## 🛠️ Mandatory Workflow

### Stage 1: Impact Analysis (The "Think" Phase)
Before writing a single line of code, you MUST output a **Technical Impact Map**:
1. **Data Path Trace**: Trace variable flow from `routes` $\rightarrow$ `service` $\rightarrow$ `redis/api`.
2. **Type Verification**: Check if you are dealing with a `dict`, `dataclass`, or `Flask Config` object.
3. **Dependency Audit**: Explicitly list every new import required.
4. **Side-Effect Mapping**: Identify every other endpoint that shares the modified function.

### Stage 2: Static Verification (The "Audit" Phase)
After writing code, perform this checklist:
- [ ] **Imports**: All used modules are imported. No `ImportError` possible.
- [ ] **Config Access**: Use `current_app.config.get("KEY")`. NEVER use `.config["KEY"]` or `.config.KEY`.
- [ ] **Exception Precision**: No `except Exception:`. Use `(ConnectionError, TimeoutError)` etc.
- [ ] **Consistency**: Does the implementation match the plan 1:1?

### Stage 3: State Sync (The "Lock" Phase)
A task is only "Complete" when:
- [ ] The code is verified.
- [ ] `SOT_MASTER.md` is updated with the technical change.
- [ ] The project is rebuilt/pushed to GitHub.

## 🚨 Failure Triggers
If you enter a "Fixed it $\rightarrow$ Broke it $\rightarrow$ Fixed it" loop, you have failed. Stop, revert to the last stable commit, and restart from Stage 1.
