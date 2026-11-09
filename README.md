# IFSCodeGuardBot

Telegramグループの新規参加者をGoogleスプレッドシートのnumeric user IDと照合し、一致したアカウントを`ban_chat_member`で無期限BANするPython Botです。Python 3.13以降に対応し、pollingで動作します。python-telegram-botは実装時点の最新版22.8を使用しています。

## セットアップ

### 1. Google Sheets

1. Google Cloudプロジェクトで **Google Sheets API** を有効にします。
2. サービスアカウントを作成し、JSON鍵をダウンロードします。
3. JSONの`client_email`に記載されたメールアドレスへ、対象スプレッドシートを **閲覧者** として共有します。一般公開は不要です。
4. `blacklist`ワークシートのA1:B1に、以下のヘッダーを設定します。

| numeric user ID | remarks |
| --- | --- |
| 123456789 | 管理用メモ |
| 987654321 | 管理用メモ |

判定はnumeric user IDの完全一致のみです。username、表示名、remarksは判定に使いません。IDは正の整数で入力し、文字列形式の数字も使用できます。数値セルは`UNFORMATTED_VALUE`で取得するため、桁区切り表示に影響されません。既にシート上で丸められた値は復元できません。

重複IDは1件として数え、IDが空欄の行は無視します。不正なIDやヘッダーがある場合は更新全体を失敗扱いにし、直前のキャッシュを維持します。ヘッダーのみのシートは有効な0件のブラックリストです。ヘッダーまで削除すると取得失敗になります。

### 2. Telegram

1. [@BotFather](https://t.me/BotFather)でBotを作成し、トークンを取得します。
2. Botを対象グループに追加し、**管理者** に設定します。
3. **ユーザーをBAN／制限する権限**（`can_restrict_members`）を付与します。

参加検知には`chat_member`更新を使用します。Botが管理者であることと、`allowed_updates`での明示的な購読が必要です。本実装では購読設定を行います。複数人が同時に追加された場合も、それぞれのメンバー更新を処理します。通常のメッセージ送信者を新規参加者とみなすことはありません。

自分自身は除外し、他のBotは通常ユーザーと同じID判定を行います。Telegramの管理者・所有者はBANできないため、一致した場合は`BAN FAILED`を記録します。自動で管理者権限を剥奪することはありません。

### 3. Python環境と設定（Linux）

Python 3.13以降がインストールされた環境で実行します。

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp config.example.yaml config.yaml
mkdir -p secrets logs
chmod 700 secrets
```

ダウンロードしたJSON鍵を`secrets/service-account.json`へ配置し、`config.yaml`のBot TokenとスプレッドシートURLを設定してください。

```bash
chmod 600 config.yaml secrets/service-account.json
python -m ifscodeguard --config ./config.yaml
```

Ctrl+CまたはSIGTERMで終了します。同じトークンで複数プロセスを起動しないでください。既存webhookはpolling開始時に削除されます。

`config.yaml`、`secrets/`、ログ、仮想環境は`.gitignore`対象です。鍵を別の場所・別名で保存する場合も、コミット対象にしないでください。実際の秘密情報はこのリポジトリに含めません。

## 設定

設定例は[config.example.yaml](config.example.yaml)を参照してください。相対パスは **configファイルのあるディレクトリ** から解決します。

| 設定 | 既定値 | 内容 |
| --- | --- | --- |
| `telegram.bot_token` | 必須 | BotFatherのトークン |
| `google.spreadsheet_url` | 必須 | 対象スプレッドシートのURL |
| `google.worksheet_name` | `blacklist` | ワークシート名 |
| `google.service_account_file` | `./secrets/service-account.json` | サービスアカウントJSON鍵 |
| `google.request_timeout` | `15` | Google API・認証HTTPリクエストのタイムアウト秒数 |
| `blacklist.refresh_interval` | `60` | 定期取得の待機間隔（秒） |
| `logging.level` | `INFO` | ログレベル |
| `logging.file` | `./logs/ifscodeguard.log` | ログファイル |
| `logging.max_bytes` | `10485760` | 1ファイルの最大バイト数 |
| `logging.backup_count` | `5` | ローテーション世代数 |

件数・間隔・タイムアウトには正の整数を指定します。起動時にYAML設定を検証します。`WARNING`以上のログレベルにすると、正常参加や更新成功などのINFOログは出力されません。通常は`INFO`を推奨します。

## キャッシュと障害時の動作

- 起動時にGoogle Sheetsを読み込み、その後は指定間隔で更新します。更新完了から次の更新まで指定秒数待機します。
- Sheetsアクセスはワーカースレッドで実行します。参加判定はメモリ内のID集合だけを参照し、Google APIを呼びません。BANや管理者認証にはTelegram APIを使用します。
- 正常取得したデータだけを一括でキャッシュへ反映します。定期更新と手動更新は直列化し、更新中も参加者は直前のキャッシュで判定します。
- 取得失敗時はWARNINGを記録してキャッシュを保持し、次回も更新を試みます。初回取得が未成功の場合は`NO USABLE CACHE`をERRORに記録します。この状態では参加者をBANできず、各参加を`JOIN UNCHECKED`としてWARNINGに記録します。
- キャッシュはメモリのみで、再起動すると失われます。管理コマンドで最終成功日時と取得失敗状態を確認できます。
- 新規参加時にのみ判定します。更新時の既存メンバーの一括BANや、シートから削除したユーザーの自動BAN解除は行いません。
- BAN時のAPIエラーや権限不足は`BAN FAILED`として記録し、他のユーザーの処理を継続します。失敗したBANの自動再試行はありません。

## 管理コマンド

対象グループ内の管理者・所有者だけが実行できます。実行のたびにTelegram APIで権限を確認し、認証に失敗した場合は拒否します。匿名管理者は個人を確認できないため、自分のアカウント名で送信してください。

`/blacklist_reload`

```text
Blacklist reloaded. 125 entries loaded.
```

失敗時は次のメッセージを返します。初回取得前でキャッシュが存在しない場合は、その旨の警告も追記します。

```text
Failed to reload blacklist. Using previous cache.
```

`/blacklist_status`

```text
IFSCodeGuardBot status

Blacklist entries: 125
Last updated: 2026-11-09 00:00:00 UTC
Cache status: OK
```

最終更新日時は **最終成功時刻（UTC）** です。失敗後は`Using previous cache (last refresh failed)`、一度も成功していない場合は`UNAVAILABLE (no usable cache)`を表示します。キャッシュはBot全体で共有されるため、どの参加グループの管理者がリロードしても同じキャッシュを更新します。

## ログ

Python標準loggingで標準出力とUTF-8ログファイルの両方に出力します。既定は10 MiB × 本体1個＋バックアップ5世代です。

```text
JOIN chat_id=-1001234567890 user_id=123456 username=@example display_name="Example User" is_bot=false
BLACKLIST MATCH chat_id=-1001234567890 user_id=987654 username=- display_name="Example" is_bot=false
BAN SUCCESS chat_id=-1001234567890 user_id=987654
BAN FAILED chat_id=-1001234567890 user_id=987654 error="Bot lacks administrator/BAN permission."
BLACKLIST REFRESH rows=125
```

実際には時刻・レベル・モジュール名も付与されます。usernameがない場合は`-`、last_nameがない場合はfirst_nameだけを記録します。表示名やエラーはJSON文字列として引用し、改行をエスケープします。トークンはログ整形時にマスクします。

## systemdによる常駐

配置先を`/opt/ifscodeguard`とする例です。ソース一式と設定・鍵を配置し、そのディレクトリ内に上記手順で仮想環境を作成してください。専用ユーザーが設定・鍵・仮想環境を読み取れるようにします。

```bash
sudo useradd --system --home-dir /opt/ifscodeguard --shell /usr/sbin/nologin ifscodeguard
sudo install -d -o ifscodeguard -g ifscodeguard -m 750 /opt/ifscodeguard/logs
sudo chown root:ifscodeguard /opt/ifscodeguard/config.yaml /opt/ifscodeguard/secrets/service-account.json
sudo chmod 640 /opt/ifscodeguard/config.yaml /opt/ifscodeguard/secrets/service-account.json
sudo chown root:ifscodeguard /opt/ifscodeguard/secrets
sudo chmod 750 /opt/ifscodeguard/secrets
sudo cp /opt/ifscodeguard/deploy/ifscodeguard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ifscodeguard
sudo systemctl status ifscodeguard
sudo journalctl -u ifscodeguard -f
```

配置先やログディレクトリを変更した場合は、serviceファイルの`WorkingDirectory`、`ExecStart`、`ReadWritePaths`も変更します。`ProtectSystem=strict`のため、ログの書き込み先を`ReadWritePaths`で明示する必要があります。設定変更後は`sudo systemctl restart ifscodeguard`で反映します。

## テスト

外部APIをモックし、実際のBAN・Sheetsアクセスを行わずに検証できます。

```bash
python -m unittest discover -s tests -v
```

本番導入前にテスト用グループで、参加イベント、BAN権限、シートの閲覧権限も確認してください。

## 参照

- [python-telegram-bot 22.8](https://pypi.org/project/python-telegram-bot/22.8/)
- [公式のメンバー更新検知例](https://docs.python-telegram-bot.org/en/stable/examples.chatmemberbot.html)
- [Telegram banChatMember](https://core.telegram.org/bots/api#banchatmember)
- [Google Sheets values.get](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/get)
