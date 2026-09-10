# Naver Maps crawler

`naver_map_crawler.py`는 2022년 Selenium 예제처럼 네이버 지도 검색 결과와 업체 상세 화면을 브라우저로 열어 공개 정보를 수집하지만, 현재 프로젝트에서는 Playwright를 사용합니다.

고정 class 하나에만 의존하지 않고 다음 순서로 동작합니다.

1. `https://map.naver.com/p/search/{query}` 열기
2. `searchIframe`을 우선 찾고 URL 패턴으로 fallback
3. 검색 결과의 `/place/{id}` 또는 `/restaurant/{id}` 링크 수집
4. 업체 상세의 `entryIframe`을 우선 찾고 URL 패턴으로 fallback
5. 상호명, 카테고리, 주소, 전화번호, 리뷰 수, 별점(노출되는 경우), 이미지 추출
6. `메뉴` 탭이 있으면 눌러 `메뉴명 + 가격` 쌍 추출
7. 동일 네이버 place id는 같은 Firestore 문서로 upsert
8. `contentHash`가 같으면 Firestore write 생략

네이버 화면 구조는 비공개 DOM이므로 언제든 바뀔 수 있습니다. 코드에는 오래된 class 이름 일부를 fallback으로 포함하지만, iframe 이름과 URL 패턴, 텍스트 기반 파싱을 함께 사용해 한 selector 변경으로 전부 깨지는 것을 줄였습니다.

## GitHub Secret

Repository -> Settings -> Secrets and variables -> Actions 에 아래 Secret을 추가합니다.

### `NAVER_CRAWL_CONFIG_JSON`

```json
{
  "queries": [
    "서울역 맛집",
    "성수동 맛집"
  ],
  "max_results_per_query": 8,
  "include_menu": true,
  "delay_ms": 1400,
  "navigation_timeout_ms": 30000,
  "collection": "crawler_items"
}
```

실제 검색어는 public repository에 올리지 않고 Secret에만 넣을 수 있습니다.

Firebase 저장까지 할 경우 기존 `FIREBASE_SERVICE_ACCOUNT_JSON` Secret도 필요합니다.

## 로컬 실행

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item config\naver.example.json config\naver.json
python naver_map_crawler.py --dry-run --headed
```

특정 검색어 하나만 테스트:

```powershell
python naver_map_crawler.py --query "서울역 맛집" --max-results 3 --dry-run --headed
```

### Ubuntu

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
cp config/naver.example.json config/naver.json
python naver_map_crawler.py --dry-run
```

## GitHub Actions

`.github/workflows/naver-map.yml`이 기본 6시간마다 실행됩니다.

Actions -> `Crawl Naver Maps` -> `Run workflow`에서 검색어 한 개와 최대 결과 수를 넣어 수동 실행할 수도 있습니다. `dry_run=true`로 실행하면 Firestore에는 쓰지 않고 로그에 JSON만 출력합니다.

## Firestore 예시

```json
{
  "kind": "naver_restaurant",
  "title": "식당 이름",
  "url": "https://map.naver.com/p/entry/place/123456",
  "naverPlaceId": "123456",
  "category": "한식",
  "address": "서울 ...",
  "phone": "02-000-0000",
  "visitorReviewCount": 1234,
  "blogReviewCount": 321,
  "imageUrl": "https://...pstatic.net/...",
  "menus": [
    {"name": "메뉴 A", "price": "10,000원"}
  ],
  "matchedQueries": ["서울역 맛집"],
  "sourceKey": "naver-map",
  "contentHash": "...",
  "createdAt": "Firestore Timestamp",
  "updatedAt": "Firestore Timestamp"
}
```

네이버에서 별점을 표시하지 않는 업체라면 `rating` 필드는 저장되지 않습니다. 전화번호, 메뉴, 이미지도 페이지에 노출되지 않으면 생략됩니다.

## 운영 제한

- 낮은 빈도로 실행하고 `delay_ms`를 너무 작게 낮추지 마세요.
- 로그인 우회, CAPTCHA 우회, 차단 회피는 구현하지 않습니다.
- 네이버가 접근 제한 또는 CAPTCHA를 보여주면 해당 실행을 중단합니다.
- 공개 화면에 노출되는 데이터만 대상으로 하며 대상 서비스의 이용약관과 정책을 확인해야 합니다.
- DOM 변경으로 검색 결과를 찾지 못하면 실패 로그를 남기므로 selector/fallback을 갱신해야 합니다.
