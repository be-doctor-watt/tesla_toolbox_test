#!/usr/bin/env python3
"""Corpus synthétique imitant la structure Toolbox, pour valider le pipeline
sans toucher aux vraies données. Génère fake_corpus.jsonl."""

import json
import sys

ARTICLES = [
    (6050000, "BMS_a066_SOC_Imbalance_Warning", ["BMS_a066"],
     "BMS_a066 is set when the battery management system detects a state of charge "
     "imbalance between bricks exceeding the calibration threshold. Verify brick "
     "voltage deltas using the diagnostic tool. If the delta exceeds 0.15 V after a "
     "full balancing cycle, the affected module must be replaced. This alert commonly "
     "follows a module replacement where the new module was not capacity matched."),
    (6050001, "BMS_a067_Brick_Overvoltage", ["BMS_a067"],
     "BMS_a067 indicates brick overvoltage during charging. Unlike an imbalance "
     "condition, this alert latches immediately and inhibits charging. Inspect the "
     "sense harness for chafing at the module interface before condemning the pack."),
    (6050002, "BMS_a066 recurrence after module swap", ["BMS_a066", "BMS_a079"],
     "If BMS_a066 returns within 200 miles of a module replacement, the replacement "
     "module capacity was likely mismatched to the pack. Perform a capacity check on "
     "all modules. BMS_a079 may accompany this condition when the imbalance is severe "
     "enough to trigger a contactor derate."),
    (6050003, "DI_a175_Drive_Inverter_Overtemp", ["DI_a175"],
     "DI_a175 is logged when the drive inverter exceeds thermal limits. Check coolant "
     "level and verify the pump is commanded on. Power is derated progressively."),
    (6050004, "CP_a004_Charge_Port_Latch_Fault", ["CP_a004"],
     "CP_a004 indicates the charge port latch actuator did not reach its commanded "
     "position. Inspect for debris and verify the actuator harness connector is seated."),
    (6050005, "Coolant service procedure", [],
     "General coolant fill and bleed procedure for the high voltage battery loop. "
     "Use only Tesla approved coolant. Bleed at the highest point of the circuit."),
]


def main(out="fake_corpus.jsonl"):
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        for aid, title, codes, body in ARTICLES:
            # 2 chunks par article pour exercer la déduplication par article_id
            for i, part in enumerate([body[:len(body) // 2], body[len(body) // 2:]]):
                f.write(json.dumps({
                    "id": f"{aid}#{i}",
                    "article_id": aid,
                    "title": title,
                    "fault_codes": codes,
                    "url": f"https://toolbox.tesla.com/articles/{aid}",
                    "text": f"{title}\n\n{part}",
                }, ensure_ascii=False) + "\n")
                n += 1
    print(f"{n} chunks -> {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fake_corpus.jsonl")
