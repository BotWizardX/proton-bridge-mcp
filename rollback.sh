#!/bin/bash
# Rollback for the Proton Bridge MCP connector. Default prints help only.
# Quick stop needs no confirmation:   rollback.sh stop
# Full removal needs a confirmation:  rollback.sh full --confirm "<date time note>" (0-9 A-Z a-z : space -, max 40)
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
case "${1:-help}" in
  stop)
    "$DIR/start.sh" stop; echo "Bridge stopped. Remove the connector entries (one per profile) from your MCP client config." ;;
  full)
    if [ "${2:-}" != "--confirm" ] || [ -z "${3:-}" ]; then echo "Refused: full removal needs --confirm \"<date time note>\"."; exit 2; fi
    # The note is only a date, time and short id: [0-9A-Za-z: -], 40 characters at most.
    if ! [[ "$3" =~ ^[0-9A-Za-z:\ -]{1,40}$ ]]; then echo "Refused: the confirmation note may hold only date, time and a short id (0-9 A-Z a-z : space -), max 40."; exit 2; fi
    mkdir -p "$DIR/run"; echo "Full removal confirmed: $3" >> "$DIR/run/rollback.log"
    "$DIR/start.sh" stop
    echo "1) Remove the connector entries (one per profile) from your MCP client config."
    echo "2) In 'start.sh cli' type: logout, then delete (removes the account from Bridge's keychain). Then exit."
    read -r -p "Done with step 1 and 2? (yes/no) " a; [ "$a" = yes ] || exit 1
    sudo apt-get remove -y protonmail-bridge
    rm -rf ~/.config/protonmail ~/.cache/protonmail ~/.local/share/protonmail
    pass rm -r -f proton-bridge 2>/dev/null
    # Bridge's own entries (docker-credential-helpers pass layout; exact path recorded at setup in run/bridge-pass-path)
    # Only a non-empty relative path naming docker-credential-helpers or bridge-v3, with no '..' and no leading '/'.
    if [ -f "$DIR/run/bridge-pass-path" ]; then
      BP="$(head -n 1 "$DIR/run/bridge-pass-path" | tr -d '\r')"
      if [ -n "$BP" ] && [[ "$BP" != /* ]] && [[ "$BP" != *..* ]] && [[ "$BP" == *docker-credential-helpers* || "$BP" == *bridge-v3* ]]; then
        pass rm -r -f "$BP" 2>/dev/null
      else
        echo "WARNING: run/bridge-pass-path does not hold a safe Bridge path; skipped. Remove Bridge's pass entries by hand."
      fi
    else
      echo "WARNING: run/bridge-pass-path is missing; Bridge's pass entries were not removed. Check with 'pass ls' by hand."
    fi
    # Only an exact 40-hex-character fingerprint is used; anything else is skipped with a warning.
    if [ -f "$DIR/run/gpg-key-fpr" ]; then
      FPR="$(head -n 1 "$DIR/run/gpg-key-fpr" | tr -d '\r')"
      if [[ "$FPR" =~ ^[0-9A-Fa-f]{40}$ ]]; then gpg --batch --yes --delete-secret-and-public-key "$FPR"
      else echo "WARNING: run/gpg-key-fpr is not a 40-hex fingerprint; GPG key not deleted. Remove it by hand."; fi
    else
      echo "WARNING: run/gpg-key-fpr is missing; GPG key not deleted. Check with 'gpg -K' by hand."
    fi
    # Remove the store only when no *.gpg entries are left on disk (never judged from 'pass ls' output).
    if [ -d ~/.password-store ]; then
      if [ -z "$(find ~/.password-store -type f -name '*.gpg' -print -quit 2>/dev/null)" ]; then rm -rf ~/.password-store
      else echo "WARNING: ~/.password-store still holds entries; left in place."; fi
    fi
    # Proton's debsig policy and keyring, if you added them for the .deb signature check (see README).
    sudo rm -rf /etc/debsig/policies/E2C75D68E6234B07 /usr/share/debsig/keyrings/E2C75D68E6234B07
    echo "Left in place on purpose: the pass and debsig-verify packages and any other packages you installed for Bridge (remove with apt if wanted), and this connector's state folder with its logs and tap ledger (remove by hand if wanted)."
    echo "3) Sign out the Bridge session in Proton web settings (session list) to kill the Bridge password." ;;
  *) sed -n 2,4p "$0" ;;
esac
