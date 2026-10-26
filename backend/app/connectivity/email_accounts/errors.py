"""One error taxonomy for every mailbox provider.

Adapters raise EmailProviderError(code); the service maps the code to a
connection status and the user-facing message below. A raw provider message
(which can echo an address or a query) is never stored nor shown.
"""

# code -> (category shown to the user, connection status it implies or None, message)
ERRORS: dict[str, tuple[str, str | None, str]] = {
    "oauth_config_missing": ("Configuration OAuth", "not_configured",
                             "Le fournisseur n'est pas configuré sur le serveur."),
    "oauth_config_invalid": ("Configuration OAuth", "config_required",
                             "La configuration OAuth du serveur est incomplète ou invalide."),
    "provider_disabled": ("Fournisseur désactivé", "not_configured",
                          "Ce fournisseur n'est pas encore activé."),
    "oauth_state_invalid": ("OAuth", None, "La demande d'autorisation est invalide ; recommencez la connexion."),
    "oauth_state_expired": ("OAuth", None, "La demande d'autorisation a expiré ; recommencez la connexion."),
    "oauth_browser_mismatch": ("OAuth", None,
                               "La connexion a été terminée dans un autre navigateur ; recommencez depuis ORBIT."),
    "oauth_denied": ("OAuth", None, "L'autorisation a été refusée chez le fournisseur."),
    "oauth_exchange_failed": ("OAuth", None, "Le fournisseur a refusé l'échange du code d'autorisation."),
    "permission_missing": ("Permission", "authorization_required",
                           "La permission de lecture des emails n'a pas été accordée."),
    "account_mismatch": ("OAuth", None, "Le compte choisi chez le fournisseur n'est pas celui à reconnecter."),
    "token_expired": ("Jeton", "token_expired_or_revoked", "L'autorisation a expiré ; reconnectez le compte."),
    "token_revoked": ("Jeton", "token_expired_or_revoked",
                      "L'autorisation a été révoquée ou n'est plus valide ; reconnectez le compte."),
    "token_unreadable": ("Jeton", "token_expired_or_revoked",
                         "Le jeton enregistré est illisible (clé de chiffrement changée ?) ; reconnectez le compte."),
    "tenant_policy_blocked": ("Politique Microsoft", "policy_blocked",
                              "Votre organisation exige l'approbation d'un administrateur pour cette application."),
    "provider_rate_limited": ("Quota fournisseur", "temporary_error",
                              "Le fournisseur limite temporairement les requêtes ; réessayez dans un instant."),
    "provider_unavailable": ("Fournisseur indisponible", "temporary_error",
                             "Le service du fournisseur est momentanément indisponible."),
    "network_error": ("Réseau", "temporary_error", "Le fournisseur est injoignable (réseau)."),
    "timeout": ("Réseau", "temporary_error", "Le fournisseur n'a pas répondu à temps."),
    "message_not_found": ("Message", None, "Ce message est introuvable (supprimé ou déplacé ?)."),
    "invalid_request": ("Requête", None, "Requête invalide."),
    "account_not_found": ("Compte", None, "Compte email introuvable."),
    "account_disabled": ("Compte", None, "Ce compte email est désactivé."),
}


class EmailProviderError(Exception):
    def __init__(self, code: str, *, retry_after: float | None = None):
        if code not in ERRORS:
            code = "provider_unavailable"
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after

    @property
    def category(self) -> str:
        return ERRORS[self.code][0]

    @property
    def status(self) -> str | None:
        return ERRORS[self.code][1]

    @property
    def user_message(self) -> str:
        return ERRORS[self.code][2]


def describe(code: str | None) -> dict | None:
    if not code:
        return None
    category, _, message = ERRORS.get(code, ERRORS["provider_unavailable"])
    return {"code": code, "category": category, "message": message}
