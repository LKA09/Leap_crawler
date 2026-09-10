# Leap Crawler

GitHub Actions에서 주기적으로 HTML 페이지를 수집하고, 정규화한 데이터를 Firebase Firestore에 저장하는 LEAP용 경량 크롤러입니다.

Firebase Cloud Functions / Cloud Scheduler를 사용하지 않으므로 Firebase 프로젝트는 Spark 요금제를 유지할 수 있습니다. 실제 크롤링 대상 URL과 CSS selector, Firebase 서비스 계정 JSON은 GitHub Actions Secrets로 주입할 수 있어 public repository에 노출할 필요가 없습니다.

## 구조

```text
GitHub Actions (매시 17분)
        |
        v
  requests + BeautifulSoup
        |
        v
   변경 여부 비교
        |
        v
Firestore crawler_items
```

- 동일 항목은 `sourceKey + url/id/title`을 SHA-256으로 해시한 안정적인 문서 ID를 사용합니다.
- 기존 `contentHash`와 동일하면 Firestore write를 하지 않습니다.
- 실행 상태는 `crawler_runs/{sourceKey}`에 기록됩니다.
- 기본 최대 수집량은 source당 200개이며, 하드 리밋은 1,000개입니다.
- `robots.txt`를 기본적으로 확인합니다.
- HTTP 429/5xx는 지수 backoff와 함께 재시도합니다.
- GitHub Actions workflow에는 수동 실행(`workflow_dispatch`)도 포함되어 있습니다.

## 필요한 GitHub Secrets

Repository -> Settings -> Secrets and variables -> Actions 에 다음 두 개를 추가합니다.

### `FIREBASE_SERVICE_ACCOUNT_JSON`

Firebase 프로젝트에서 발급한 서비스 계정 JSON 전체를 한 줄/여러 줄 그대로 저장합니다.

예:

```json
{
  "type": "service_account",
  "project_id": "leap-9e2ec",
  "private_key_id": "...",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
  "client_email": "...",
  "client_id": "..."
}
```

서비스 계정 JSON 파일 자체는 절대 repository에 commit하지 마세요.

### `CRAWLER_SOURCES_JSON`

실제 대상 사이트와 selector를 public repository에 남기고 싶지 않을 때 이 Secret에 설정합니다.

```json
{
  "sources": [
    {
      "key": "my-source",
      "enabled": true,
      "url": "https://example.com/list",
      "collection": "crawler_items",
      "item_selector": "article.card",
      "max_items": 100,
      "fields": {
        "title": {
          "selector": "h2",
          "type": "text",
          "required": true
        },
        "url": {
          "selector": "a",
          "type": "attr",
          "attr": "href",
          "absolute_url": true,
          "required": true
        },
        "summary": {
          "selector": ".summary",
          "type": "text"
        },
        "imageUrl": {
          "selector": "img",
          "type": "attr",
          "attr": "src",
          "absolute_url": true
        },
        "publishedAt": {
          "selector": "time",
          "type": "attr",
          "attr": "datetime"
        }
      }
    }
  ]
}
```

Secret이 없으면 로컬의 `config/sources.json`을 읽습니다. 형식 참고용 파일은 `config/sources.example.json`입니다.

## Firestore 문서 예시

`crawler_items/{sha256-id}`

```json
{
  "title": "Example title",
  "url": "https://example.com/item/1",
  "summary": "...",
  "imageUrl": "https://example.com/image.png",
  "publishedAt": "2026-09-10T00:00:00Z",
  "sourceKey": "my-source",
  "sourcePage": "https://example.com/list",
  "contentHash": "...",
  "createdAt": "Firestore Timestamp",
  "updatedAt": "Firestore Timestamp"
}
```

`crawler_runs/{sourceKey}`에는 최근 실행의 `status`, `fetched`, `created`, `updated`, `unchanged`, `lastRunAt`, `error`가 저장됩니다.

## 로컬 실행

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item config\sources.example.json config\sources.json
python crawler.py --dry-run
```

Ubuntu:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/sources.example.json config/sources.json
python crawler.py --dry-run
```

`--dry-run`은 Firestore에 접근하지 않고 최대 5개 파싱 결과를 출력합니다.

특정 source만 실행:

```bash
python crawler.py --source my-source
```

## GitHub Actions

`.github/workflows/crawl.yml`은 매시 17분에 실행됩니다.

```yaml
- cron: "17 * * * *"
```

Actions 탭에서 `Crawl and sync Firestore` -> `Run workflow`를 누르면 즉시 수동 실행할 수 있으며, source key를 입력하면 해당 source만 실행합니다.

## 테스트

```bash
python -m unittest discover -s tests -v
```

GitHub Actions에서도 실제 크롤링 전에 parser 테스트를 먼저 실행합니다.

## Firestore 보안

이 크롤러는 Firebase Admin SDK를 사용하므로 Firestore Security Rules의 일반 클라이언트 write 권한에 의존하지 않습니다. 앱에서는 `crawler_items`를 읽기 전용으로 취급하고 클라이언트 write는 차단하는 것을 권장합니다.

LEAP 본체의 `firestore.rules`는 생성 파일이므로 직접 수정하지 말고 `scripts/generate-rules.mjs`를 수정한 뒤 규칙을 다시 생성/테스트해야 합니다.

예를 들어 로그인 사용자만 읽게 할 경우 생성되는 규칙의 개념은 다음과 같습니다.

```text
match /crawler_items/{id} {
  allow read: if signedIn();
  allow write: if false;
}

match /crawler_runs/{id} {
  allow read, write: if false;
}
```

공개 페이지에서 로그인 없이 보여줄 데이터라면 `crawler_items`의 read 조건만 제품 요구사항에 맞춰 `true`로 바꿀 수 있습니다.

## 주의사항

- 대상 사이트의 이용약관, robots.txt, 저작권 및 접근 제한을 준수하세요.
- 로그인 우회, CAPTCHA 우회, 차단 회피 기능은 포함하지 않습니다.
- 현재 엔진은 서버 렌더링된 HTML용입니다. 콘텐츠가 브라우저 JavaScript 실행 후에만 나타나는 사이트라면 Playwright 기반 source adapter를 추가해야 합니다.
- GitHub Actions schedule은 정확히 해당 분에 시작된다는 보장은 없으므로 초 단위 실시간 수집에는 적합하지 않습니다.
