"""Folder convention for personal documents in a synced workspace:

    Personnel/<identifiant>/...      (also: Employés, Salariés, Personal, Employees, Staff)

anywhere in the tree ("RH/Personnel/tarek/fiche_paie_09.pdf" works too).
<identifiant> is a username or a full name, compared without case, accents
or separators ("tarek", "Tarek Chebbah", "tarek_chebbah"). A folder that
matches no active user is just a folder: nothing is guessed.
"""

import re
import unicodedata

from sqlalchemy.orm import Session

from app.models.user import User

PERSONAL_ROOTS = frozenset({"personnel", "employes", "salaries", "personal", "employees", "staff"})


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in decomposed if not unicodedata.combining(c)).lower())


def personal_subject_for_path(db: Session, relative_path: str) -> User | None:
    parts = relative_path.split("/")
    # <root>/<who>/<file> at least: the user folder must contain the file.
    for i in range(len(parts) - 2):
        if _fold(parts[i]) not in PERSONAL_ROOTS:
            continue
        who = _fold(parts[i + 1])
        if not who:
            continue
        for user in db.query(User).filter(User.is_active.is_(True)):
            if who in (_fold(user.username), _fold(user.full_name)):
                return user
    return None
