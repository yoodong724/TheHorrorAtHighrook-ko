# The Horror at Highrook 한글패치

Windows Steam판 1.06 / build 18387525용입니다.

- `src/`: 번역 데이터와 패치 빌드 소스. 빌드는 [BUILD.md](BUILD.md)를 참고하세요.
- `installer/`: Steam 자동 탐색 설치·복원 스크립트와 폰트 라이선스.
- [Releases](https://github.com/yoodong724/TheHorrorAtHighrook-ko/releases): 설치용 한글패치 ZIP.

1. 게임을 종료하고 Releases에서 `Highrook-ko-*.zip`을 내려받아 압축을 풉니다.
2. `install-steam.cmd`를 실행합니다. 내부 `highrook-ko-test.patch.zip`은 풀지 않습니다.
3. `installed_in_steam`이 표시되면 Steam에서 게임을 실행합니다.

제거하려면 게임을 종료하고 `restore-steam.cmd`를 실행하세요. 복원할 때까지 게임 폴더의 `.highrook-ko-backup`을 보관하세요. 복원 후 재설치하려면 복원이 성공한 것을 확인하고 `.highrook-ko-backup`을 다른 위치에 보관한 뒤 진행하세요.

자동 탐색이 실패하면 Steam 라이브러리 경로를 지정하세요. 복원할 때는 `Install`을 `Restore`로 바꿉니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\steam-auto.ps1 -Action Install -SteamRoot "D:\SteamLibrary"
```
