# Copyright (c) 2026 Andrew Murphy
#
# This file is part of coding-agent-useful-tools.
#
# coding-agent-useful-tools is dual-licensed:
# 1. Under the GNU Affero General Public License v3.0 (AGPLv3) for open-source
#    and non-proprietary use. See the LICENSE file in the root directory.
# 2. Under a Commercial License for proprietary use or without AGPLv3 
#    obligations. See COMMERCIAL-LICENSE.md or contact andrew_murphy_datapro@outlook.com
#     with subject line: "Commercial Licensing Request for coding-agent-useful-tools".

"""Repository-wide regression guard: never commit credentials or account IDs."""
from pathlib import Path
import re
import unittest
from agentlog.core.redact import redact

class RepositoryPrivacyTests(unittest.TestCase):
    def test_repository_has_no_unredacted_sensitive_values(self):
        root=Path(__file__).resolve().parents[3]
        # The license template's public contact attribution must be preserved.
        template=(root/'file_header_template.txt').read_text(encoding='utf-8')
        licensed_contacts=re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",template)
        failures=[]
        for path in sorted(root.rglob('*')):
            if not path.is_file() or '.git' in path.relative_to(root).parts:continue
            raw=path.read_bytes()
            if b'\0' in raw:continue  # Binary artifacts are not source or fixture text.
            text=raw.decode('utf-8',errors='replace')
            for contact in licensed_contacts:text=text.replace(contact,'<license-contact>')
            text=re.sub(r"(AccountKey|SharedAccessSignature)=(?=\[)",r"\1<regex-equals>",text)
            _,findings=redact(text)
            if findings:failures.append((path.relative_to(root).as_posix(),findings))
        self.assertEqual(failures,[],"Sensitive patterns found (paths and kind counts only): "+repr(failures))
