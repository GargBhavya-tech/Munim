#!/usr/bin/env bash
set -euo pipefail
hook=.git/hooks/pre-commit
printf '#!/usr/bin/env bash\nexec scripts/check_secrets.sh\n' > "$hook"
chmod +x "$hook"
echo "pre-commit secret check installed"
