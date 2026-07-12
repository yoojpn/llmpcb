"""
Entry point: give it a natural-language request, it runs the full
Designer -> Layer1 -> Critic -> auto-fix loop (agents/orchestrator.run_audit_loop)
and reports the outcome. This is the actual LLMPCB pipeline, not a manual
step-by-step driver.

If the loop hits its iteration cap without resolving, the user is asked
whether to continue (optionally with extra guidance) rather than the run
simply terminating unresolved.
"""
from __future__ import annotations
import sys
import os
import json

from agents.gemini_client import LLMPCBGeminiClient
from agents.orchestrator import run_audit_loop, MAX_LOOP_ITERATIONS


def _save_log(state):
    with open("_work/run_log.json", "w", encoding="utf-8") as f:
        json.dump({
            "resolved": state.resolved,
            "iterations": state.iteration,
            "escalated_to_human": state.escalated_to_human,
            "unresolved_reason": state.unresolved_reason,
            "spice_verified": state.spice_verified,
            "history": state.history,
        }, f, ensure_ascii=False, indent=2, default=str)


def _print_status(state):
    print(f"resolved: {state.resolved}")
    print(f"iterations: {state.iteration}")
    print(f"escalated_to_human: {state.escalated_to_human}")
    print(f"spice_verified: {state.spice_verified}")
    if state.spice_verified is False:
        print("  WARNING: SPICE could not verify this design. Relying on calculator-based checks only.")
    if state.unresolved_reason:
        print(f"unresolved_reason: {state.unresolved_reason}")


def main(user_request: str, non_interactive_continue: bool = False):
    with open("prompts/designer_system.md", encoding="utf-8") as f:
        designer_prompt = f.read()
    with open("prompts/critic_system.md", encoding="utf-8") as f:
        critic_prompt = f.read()

    client = LLMPCBGeminiClient()
    spec = {"function_summary": user_request}

    def on_progress(i, current_state):
        print(f"[progress] iteration {i} starting...", flush=True)
        _save_log(current_state)

    state = run_audit_loop(
        spec, client, designer_prompt, critic_prompt, on_progress=on_progress,
    )
    _save_log(state)

    while not state.resolved and state.escalated_to_human:
        _print_status(state)
        print("\n--- history (recent) ---")
        for h in state.history[-6:]:
            print(json.dumps(h, ensure_ascii=False, default=str)[:400])

        if state.unresolved_reason and state.unresolved_reason.startswith("[UNRECOVERABLE]"):
            # This was NOT a "ran out of iterations, might succeed with
            # more" escalation -- it's a structural problem (e.g. the fixed
            # request overhead exceeds the model's TPM budget) that will
            # recur identically on the very next call after resuming.
            # Auto-continuing here (as non_interactive_continue would
            # otherwise do unconditionally) previously caused a real
            # infinite loop: total_413_count isn't reset across resumes,
            # so the immediate next 413 re-triggered this same escalation
            # forever, consuming hundreds of iterations with zero progress.
            print("\nUnrecoverable condition detected; not auto-continuing. See reason above.")
            break

        if non_interactive_continue:
            # e.g. for automated verification runs: keep going a fixed
            # number of extra iterations without a human actually typing.
            choice = "c"
        else:
            print(
                f"\nThe design is not yet resolved after {state.iteration} iterations.\n"
                "Options: [c] continue with more iterations / "
                "[g] continue with extra guidance for the Designer / [q] stop here"
            )
            choice = input("> ").strip().lower()

        if choice == "q":
            break
        guidance = None
        if choice == "g" and not non_interactive_continue:
            guidance = input("Guidance to add: ").strip()

        state = run_audit_loop(
            spec, client, designer_prompt, critic_prompt, on_progress=on_progress,
            resume_state=state, extra_iterations=MAX_LOOP_ITERATIONS, human_guidance=guidance,
        )

    _print_status(state)
    print("\n--- history ---")
    for h in state.history:
        print(json.dumps(h, ensure_ascii=False, default=str)[:500])

    _save_log(state)


if __name__ == "__main__":
    request = sys.argv[1] if len(sys.argv) > 1 else "Design a simple LED blinker circuit (Lチカ)."
    non_interactive = os.environ.get("LLMPCB_NONINTERACTIVE_CONTINUE") == "1"
    main(request, non_interactive_continue=non_interactive)
