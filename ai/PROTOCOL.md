# Zero-Regression Protocol (ZRP)

> This protocol is a mandatory operational standard for the agent. It is designed to eliminate the "Iterative Patching" loop and ensure that every change is correct the first time.

## 🛠️ The Three-Stage Workflow

### Stage 1: Impact Analysis (The "Think" Phase)
Before any code is written, the agent must provide a Technical Impact Map:
1. **Data Path Trace**: Trace every variable and function call from caller $\rightarrow$ callee.
2. **Type Verification**: Explicitly check object types (e.g., `AppConfig` dataclass vs. Flask `Config` dictionary).
3. **Dependency Audit**: List every import required for the new/moved code.
4. **Side-Effect Mapping**: Identify every other endpoint or function that will be affected by the change.

### Stage 2: Static Verification (The "Audit" Phase)
After writing code, but before declaring it "done," the agent must perform and list a manual audit:
- [ ] **Imports**: All used modules are explicitly imported in the file.
- [ ] **Config Access**: No attribute access (`.`) on `current_app.config`; use `.get()`.
- [ ] **Exception Precision**: No broad `except Exception` unless it is a global error handler.
- [ ] **Consistency**: The implementation matches the approved plan 1:1.

### Stage 3: Synchronization Lock (The "Cleanup" Phase)
A task is only "Completed" when the following state sync is finished:
- [ ] **Code**: Written, verified, and tested.
- [ ] **CHANGELOG.md**: Updated with technical details.
- [ ] **CONTEXT.md**: Updated "Active Work" and "Recent Changes".
- [ ] **project.zip**: Rebuilt to include latest changes.

---

## 🚩 Failure Triggers
If any of the following occur, the agent has violated the ZRP:
1. **The "Fixed it" Loop**: Fixing a bug created by the previous "fix."
2. **ImportErrors**: Running code that fails due to a missing import.
3. **AttributeErrors**: Trying to access a property that doesn't exist on a Flask object.
4. **Silent Failures**: Writing code that fails without a clear error message.

## 🚨 Enforcement
The user may signal a protocol violation by saying: **"You skipped the protocol."** 
Upon this signal, the agent must stop, revert to the last known stable state, and restart from Stage 1.
