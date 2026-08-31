"""Détection et normalisation des codes défaut Tesla. Zéro dépendance :
partagé entre harvest.py (qui tourne chez toi) et rag.py (l'indexation).

Format Tesla : PREFIXE_lNNN où l ∈ {a=alert, f=fault, w=warning, u=UDS}.
    BMS_a066, DI_a175, CP_a004, UI_f012, VCFRONT_a191...
"""

import re

# Le préfixe peut aller jusqu'à 8 caractères (VCFRONT, VCSEC, EPBL/EPBR,
# CHGDCDC...). Le limiter à 5 fait rater ~15 % des codes du corpus.
#
# ATTENTION aux frontières : surtout PAS de \b en fin de motif. En regex `_`
# est un caractère de mot, donc `\b` ne matche pas entre `066` et `_SOC` —
# et `BMS_a066_SOC_Imbalance_Warning`, qui est la forme exacte des titres
# Toolbox, ne serait jamais reconnu. On borne donc à droite par « pas un
# chiffre » (ce qui rejette quand même BMS_a0666) et à gauche par « pas un
# alphanumérique » (ce qui autorise un `_` juste avant le préfixe).
FAULT_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,7})_([afwu])(\d{3})(?![0-9])",
    re.IGNORECASE,
)


def norm_code(match) -> str:
    """-> forme canonique : préfixe majuscule, lettre minuscule, 3 chiffres."""
    if isinstance(match, str):
        m = FAULT_RE.fullmatch(match)
        if not m:
            return match
        match = m
    return f"{match.group(1).upper()}_{match.group(2).lower()}{match.group(3)}"


def extract_codes(text: str) -> list[str]:
    """Tous les codes distincts d'un texte, triés, en forme canonique."""
    return sorted({norm_code(m) for m in FAULT_RE.finditer(text or "")})
