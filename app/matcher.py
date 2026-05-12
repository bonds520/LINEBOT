from rapidfuzz import fuzz, process
from sqlalchemy.orm import Session
from app.models import QAPair
from typing import Optional, Tuple


MATCH_THRESHOLD = 60


def find_best_match(user_input: str, db: Session) -> Optional[Tuple[QAPair, float]]:
    qa_list = db.query(QAPair).filter(QAPair.is_active == True, QAPair.is_trained == True).all()
    if not qa_list:
        return None

    best_score = 0
    best_qa = None

    for qa in qa_list:
        candidates = [qa.question]
        if qa.keywords:
            import re
            normalized = re.sub(r'[、，；;]', ',', qa.keywords)
            candidates.extend([k.strip() for k in normalized.split(",") if k.strip()])

        for candidate in candidates:
            score = fuzz.partial_ratio(user_input, candidate)
            if score > best_score:
                best_score = score
                best_qa = qa

    if best_score >= MATCH_THRESHOLD:
        return best_qa, best_score
    return None
