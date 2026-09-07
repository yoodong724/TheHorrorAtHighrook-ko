# 패치 소스 빌드

Steam build 18387525(게임 표시 버전 1.06)의 무변경 설치 사본, Python 3.11 이상, WSL, Windows PowerShell 5.1, Mono.Cecil.dll이 필요합니다. 입력 게임 폴더는 읽기만 하며 결과는 새 출력 폴더에 생성됩니다.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python src/build_patch.py \
  --baseline "/path/to/pristine/The Horror at Highrook" \
  --output "/tmp/highrook-ko-output" \
  --cecil "/path/to/Mono.Cecil.dll"
```

빌더는 기준판 1,684개 파일의 SHA-256을 확인한 뒤 번역·이미지·글꼴을 적용합니다. 결과 폴더에는 변경 파일, 빌드 정보와 `highrook-ko-test.patch.zip`을 기록합니다. 현재 패치와 다른 출력은 거부합니다.

설치용 ZIP은 다음 명령으로 만듭니다.

```bash
python3 package.py --patch /tmp/highrook-ko-output/highrook-ko-test.patch.zip --output dist/Highrook-ko-v0.1.0-beta.1.zip
```

글꼴은 `src/assets/font/OFL.txt`의 SIL Open Font License 1.1을 따릅니다. Mono.Cecil.dll은 별도로 준비해야 합니다. 재현에 사용한 DLL의 SHA-256은 `c41bdb9ffd3c5f6e17d2382c1012d73703e035e3f1100245fdd4e08c8dc6eb5b`입니다.
