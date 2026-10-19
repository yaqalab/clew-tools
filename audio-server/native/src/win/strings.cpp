#include "strings.h"

#include <windows.h>

#include <cstdarg>
#include <cwchar>

namespace clew::win {

namespace {

struct Pair {
    S id;
    const wchar_t* en;
    const wchar_t* ja;
};

// clang-format off
const Pair kStrings[] = {
    {S::AppName, L"Clew audio server", L"Clew オーディオサーバー"},
    {S::TipRunning, L"Clew audio server — running on 127.0.0.1:%d", L"Clew オーディオサーバー — 127.0.0.1:%d で実行中"},
    {S::TipConverting, L"Clew audio server — converting %s…", L"Clew オーディオサーバー — %s を変換中…"},
    {S::TipStopped, L"Clew audio server — stopped", L"Clew オーディオサーバー — 停止中"},
    {S::TipProblem, L"Clew audio server — %s", L"Clew オーディオサーバー — %s"},
    {S::MenuStatusRunning, L"Running on 127.0.0.1:%d", L"127.0.0.1:%d で実行中"},
    {S::MenuStatusStopped, L"Stopped", L"停止中"},
    {S::MenuOpen, L"Open settings", L"設定を開く"},
    {S::MenuCopySecret, L"Copy the secret", L"シークレットをコピー"},
    {S::MenuCopyAddress, L"Copy the address", L"アドレスをコピー"},
    {S::MenuOpenFolder, L"Open the audio folder", L"音声フォルダーを開く"},
    {S::MenuStop, L"Stop the server", L"サーバーを停止"},
    {S::MenuStart, L"Start the server", L"サーバーを開始"},
    {S::MenuAutostart, L"Start with Windows", L"Windows の起動時に開始"},
    {S::MenuQuit, L"Quit", L"終了"},
    {S::StatusRunning, L"Running — http://127.0.0.1:%d · requests so far: %llu", L"実行中 — http://127.0.0.1:%d · これまでのリクエスト: %llu 件"},
    {S::StatusStopped, L"Stopped. Clew cannot reach your audio files until it starts again.", L"停止中です。再開するまで Clew から音声ファイルにアクセスできません。"},
    {S::StatusProblem, L"Not running: %s", L"停止中: %s"},
    {S::ProblemNoFolder, L"add the folder of your audio files below.", L"下で音声ファイルのフォルダーを追加してください。"},
    {S::ProblemPortInUse, L"port %d is used by another program; choose another port below.", L"ポート %d は他のプログラムが使用中です。下で別のポートを選択してください。"},
    {S::ProblemFolderGone, L"%s cannot be opened (a disconnected drive?).", L"%s を開けません（ドライブが接続されていない可能性があります）。"},
    {S::GroupFolder, L"Folders of your audio files: Clew uses the ticked ones", L"音声ファイルのフォルダー（チェックしたものを Clew で使います）"},
    {S::Browse, L"Browse…", L"参照…"},
    {S::Open, L"Open", L"開く"},
    {S::GroupConnection, L"Connection", L"接続"},
    {S::Port, L"Port", L"ポート"},
    {S::LinkHours, L"Play links last (hours)", L"再生リンクの有効期間（時間）"},
    {S::Origins, L"Other Clew addresses allowed to connect, one per line (a Clew on this computer is always allowed):", L"接続を許可する他の Clew のアドレス（1 行に 1 つ。このコンピューター上の Clew は常に許可されます）："},
    {S::GroupSecret, L"Secret", L"シークレット"},
    {S::SecretHint, L"Paste it into Clew (“From the audio server…”). Kept encrypted for your Windows account.", L"Clew の「オーディオサーバーから…」に貼り付けてください。Windows アカウント用に暗号化して保存。"},
    {S::Show, L"Show", L"表示"},
    {S::Hide, L"Hide", L"隠す"},
    {S::Copy, L"Copy", L"コピー"},
    {S::NewSecret, L"New secret…", L"作り直す…"},
    {S::NewSecretConfirm, L"Make a new secret?\n\nThe one Clew has stops working at once; you then paste the new one into Clew.", L"新しいシークレットを作成しますか？\n\nClew に保存されているシークレットはすぐに使えなくなります。新しいシークレットを Clew に貼り付けてください。"},
    {S::GroupConversion, L"Conversion", L"変換"},
    {S::Convert, L"Convert what browsers cannot play well (WMA, AMR, variable-bit-rate MP3) with Windows' own codecs", L"ブラウザーで正しく再生できないファイル（WMA、AMR、可変ビットレート MP3）を Windows 標準のコーデックで変換する"},
    {S::Prepare, L"Prepare WMA and AMR files in the background, so they open at once", L"WMA と AMR ファイルをバックグラウンドで事前に変換し、すぐに開けるようにする"},
    {S::Ffmpeg, L"ffmpeg (optional)", L"ffmpeg（任意）"},
    {S::FfmpegFind, L"Find", L"検索"},
    {S::FfmpegHint, L"Used only when Windows cannot convert a file; empty: never.", L"Windows で変換できない場合のみ使用（空欄：使用しない）。"},
    {S::FfmpegNotFound, L"No ffmpeg.exe was found beside this program or on PATH.", L"このプログラムと同じ場所や PATH に ffmpeg.exe が見つかりませんでした。"},
    {S::CacheLimit, L"Cache limit (MB)", L"キャッシュ（MB）"},
    {S::CacheUsed, L"%s in use", L"使用中: %s"},
    {S::ClearCache, L"Clear cache", L"キャッシュを消去"},
    {S::Autostart, L"Start with Windows", L"Windows の起動時に開始"},
    {S::Activity, L"Recent activity (kept in memory only, never written to disk)", L"最近のアクティビティ（メモリ上のみに保持され、ディスクには書き込まれません）"},
    {S::Save, L"Save", L"保存"},
    {S::Close, L"Close", L"閉じる"},
    {S::Saved, L"Saved.", L"保存しました。"},
    {S::Copied, L"Copied.", L"コピーしました。"},
    {S::BadPort, L"The port is a number from 1024 to 65535.", L"ポートは 1024〜65535 の数値で指定してください。"},
    {S::BadHours, L"Links last from 1 to 168 hours.", L"リンクの有効期間は 1〜168 時間で指定してください。"},
    {S::BadCache, L"The cache limit is at least 256 MB.", L"キャッシュ上限は 256 MB 以上で指定してください。"},
    {S::BadOrigin, L"Not an address: %s (write it as https://host or https://host:port).", L"アドレスではありません: %s（https://ホスト または https://ホスト:ポート の形式で入力してください）。"},
    {S::BadFolder, L"This folder does not exist.", L"このフォルダーは存在しません。"},
    {S::BadFfmpeg, L"This ffmpeg file does not exist.", L"この ffmpeg ファイルは存在しません。"},
    {S::ChooseFolder, L"Choose the folder of your audio files", L"音声ファイルのフォルダーを選択"},
    {S::ChooseFfmpeg, L"Choose ffmpeg.exe", L"ffmpeg.exe を選択"},
    {S::FirstRun, L"Add the folder of your audio files to start.", L"開始するには音声ファイルのフォルダーを追加してください。"},
    {S::AlreadyRunning, L"The Clew audio server is already running; its icon is in the notification area.", L"Clew オーディオサーバーはすでに実行中です。通知領域にアイコンがあります。"},
    {S::Start, L"Start", L"開始"},
    {S::Stop, L"Stop", L"停止"},
    {S::TabAudio, L"Audio files", L"音声ファイル"},
    {S::TabDictation, L"Dictation", L"音声入力"},
    {S::SttOff, L"Dictation is off. Start it to dictate in Clew with speech recognition on this computer.", L"音声入力は停止中です。開始すると、このコンピューター上の音声認識で Clew に音声入力できます。"},
    {S::SttMissing, L"Dictation is not installed. Run the installer again and tick “Dictation”.", L"音声入力がインストールされていません。インストーラーを再度実行し、「音声入力」を選んでください。"},
    {S::SttNoModel, L"Download the model %s below, then start.", L"下でモデル %s をダウンロードしてから開始してください。"},
    {S::SttLoading, L"Loading the model %s…", L"モデル %s を読み込み中…"},
    {S::SttReady, L"Dictation ready: %s, %s", L"音声入力の準備ができました: %s、%s"},
    {S::SttFailed, L"Dictation stopped: %s", L"音声入力が停止しました: %s"},
    {S::SttDownloading, L"Downloading %s: %d %%", L"%s をダウンロード中: %d %%"},
    {S::SttDownloadFailed, L"The download failed: %s", L"ダウンロードに失敗しました: %s"},
    {S::SttNoMemory, L"not enough memory for this model; choose small.", L"このモデルにはメモリが足りません。small を選んでください。"},
    {S::LangChosen, L"Chosen in Clew for each session", L"Clew でセッションごとに選択"},
    {S::LangEnglish, L"English", L"英語"},
    {S::LangJapanese, L"Japanese", L"日本語"},
    {S::LangFrench, L"French", L"フランス語"},
    {S::LangMalagasy, L"Malagasy", L"マダガスカル語"},
    {S::LangPerSession, L"the language chosen in Clew", L"言語は Clew で選択"},
    {S::GroupSttModel, L"Model", L"モデル"},
    {S::SttModelLine, L"%s — %s · %s · %s", L"%s — %s · %s · %s"},
    {S::SttUseLive, L"for live dictation", L"ライブ入力向け"},
    {S::SttUseFiles, L"slower, for files", L"低速、ファイル向け"},
    {S::SttUseGpu, L"with an NVIDIA graphics card", L"NVIDIA グラフィックカード向け"},
    {S::SttEnglishOnly, L" · English only", L" · 英語のみ"},
    {S::SttNeedsMemory, L"%s needs a computer with %u GB of memory; this one has %s.", L"%s には %u GB のメモリーが必要です。このコンピューターは %s です。"},
    {S::SttNeedsGpu, L"%s needs an NVIDIA graphics card with CUDA; this computer has none.", L"%s には CUDA 対応の NVIDIA グラフィックカードが必要です。このコンピューターにはありません。"},
    {S::SttSpeakersMissing, L"The speaker model for live transcription (%s) is not downloaded yet; Download fetches it.", L"ライブ文字起こし用の話者モデル（%s）はまだダウンロードされていません。「ダウンロード」で取得します。"},
    {S::SttSpeakerModel, L"the speaker model", L"話者モデル"},
    {S::SttGpuFound, L"An NVIDIA graphics card was found: models run on it when CUDA’s libraries (cuBLAS, cuDNN) are installed, else on the processor.", L"NVIDIA グラフィックカードが見つかりました。CUDA のライブラリー（cuBLAS、cuDNN）があればそこで、なければプロセッサーで動きます。"},
    {S::SttHere, L"downloaded", L"ダウンロード済み"},
    {S::SttNotHere, L"not downloaded", L"未ダウンロード"},
    {S::SttDownload, L"Download", L"ダウンロード"},
    {S::SttDownloadConfirm, L"Download the model %s (%s) into this folder?\n\n%s\n\n%s free on that drive.", L"モデル %s（%s）を次のフォルダーにダウンロードしますか？\n\n%s\n\nそのドライブの空き容量: %s"},
    {S::SttNoSpace, L"Not enough free space: %s is needed and %s is free on that drive. Choose another models folder.", L"空き容量が足りません。%s が必要ですが、そのドライブの空き容量は %s です。別のモデルフォルダーを選んでください。"},
    {S::GroupSttFolder, L"Models folder", L"モデルのフォルダー"},
    {S::Change, L"Change…", L"変更…"},
    {S::SttFree, L"%s free on this drive", L"このドライブの空き容量: %s"},
    {S::GroupSttConn, L"Language and port", L"言語とポート"},
    {S::Language, L"Language", L"言語"},
    {S::GroupSttKey, L"Key", L"キー"},
    {S::SttKeyHint, L"In Clew: Settings › Dictation › This computer, then paste it as the key. Kept encrypted for your Windows account.", L"Clew の「設定 › 音声入力 › このコンピューター」を選び、キーとして貼り付けてください。Windows アカウント用に暗号化して保存されます。"},
    {S::NewSttKeyConfirm, L"Make a new dictation key?\n\nThe one Clew has stops working at once; you then paste the new one into Clew.", L"音声入力のキーを作り直しますか？\n\nClew に設定済みのキーはすぐに使えなくなります。新しいキーを Clew に貼り付けてください。"},
    {S::SttAutostart, L"Start dictation with the server. Loading the model takes a few seconds, so starting early keeps the first sentence quick.", L"サーバーと一緒に音声入力を開始する。モデルの読み込みに数秒かかるため、早めに開始すると最初の文がすぐに入力されます。"},
    {S::SttLocal, L"Runs on this computer only: your voice is not sent anywhere.", L"このコンピューター上だけで動作します。音声はどこにも送信されません。"},
    {S::ChooseModelsFolder, L"Choose the folder for dictation models", L"音声入力モデルのフォルダーを選択"},
    {S::BadSttPort, L"The dictation port is a number from 1024 to 65535, other than the audio server's.", L"音声入力のポートは 1024〜65535 の数値で、オーディオサーバーのポート以外を指定してください。"},
    {S::NewKey, L"New key…", L"作り直す…"},
    {S::MenuDictationStart, L"Dictation: Start", L"音声入力: 開始"},
    {S::MenuDictationStop, L"Dictation: Stop", L"音声入力: 停止"},
    {S::MenuCopySttKey, L"Copy the dictation key", L"音声入力のキーをコピー"},
    {S::MenuFolders, L"Audio folders", L"音声フォルダー"},
    {S::MenuOpenOneFolder, L"Open an audio folder", L"音声フォルダーを開く"},
    {S::FolderAdd, L"Add…", L"追加…"},
    {S::FolderRemove, L"Remove", L"外す"},
    {S::FolderRemoveConfirm, L"Remove %s from the list?\n\nThe folder and its recordings stay where they are. A recording linked from it in Clew plays again once the folder is added back.", L"%s を一覧から外しますか？\n\nフォルダーと録音はそのまま残ります。Clew でこのフォルダーからリンクした録音は、フォルダーを追加し直すと再び再生できます。"},
    {S::FolderAlready, L"This folder is already in the list.", L"このフォルダーはすでに一覧にあります。"},
    {S::ColFolderName, L"Name in Clew", L"Clew での名前"},
    {S::ColFolderPath, L"Folder", L"フォルダー"},
    {S::ProblemAllOff, L"every folder is off: tick the ones Clew may use.", L"すべてのフォルダーがオフです。Clew で使うフォルダーにチェックを入れてください。"},
    {S::TabUpdates, L"Updates", L"更新"},
    {S::UpdVersion, L"Clew audio server %s, published by YaqaLab.", L"Clew オーディオサーバー %s（発行元: YaqaLab）"},
    {S::GroupUpd, L"New versions", L"新しいバージョン"},
    {S::UpdAuto, L"Check for a new version once a day", L"新しいバージョンを 1 日 1 回確認する"},
    {S::UpdCheck, L"Check now", L"今すぐ確認"},
    {S::UpdNotes, L"What's new", L"更新内容"},
    {S::UpdInstall, L"Install %s…", L"%s をインストール…"},
    {S::UpdInstallNone, L"Install…", L"インストール…"},
    {S::UpdIdle, L"Not checked yet.", L"まだ確認していません。"},
    {S::UpdChecking, L"Checking…", L"確認中…"},
    {S::UpdCurrent, L"This is the latest version.", L"最新のバージョンです。"},
    {S::UpdAvailable, L"Version %s is available. Nothing changes until you install it.", L"バージョン %s を利用できます。インストールするまで何も変わりません。"},
    {S::UpdDownloading, L"Downloading %s… %d %%", L"%s をダウンロード中… %d %%"},
    {S::UpdFailed, L"Not finished: %s. This version keeps working as before.", L"完了しませんでした: %s。現在のバージョンはこれまでどおり使えます。"},
    {S::UpdUnsigned, L"This copy is not signed, so it does not install updates itself. “What's new” opens the download page.", L"このコピーには署名がないため、自動ではインストールしません。「更新内容」からダウンロードページを開けます。"},
    {S::UpdKept, L"Installing keeps your folders, secret, dictation key and models. The server stops for a moment and starts again by itself; Clew reconnects on its own.", L"インストールしても、フォルダー、シークレット、音声入力のキー、モデルはそのまま残ります。サーバーは一時的に停止し、自動で再開します。Clew も自動で再接続します。"},
    {S::GroupUpdHow, L"How a new version is checked", L"新しいバージョンの確認方法"},
    {S::UpdHow, L"Checking asks github.com/yaqalab/clew-tools for the number of the latest version. Nothing about you, your folders or your recordings is sent; GitHub sees this computer's internet address, as any website does.\n\nNothing is downloaded or installed until you press Install. The new installer must then have exactly the size and fingerprint (SHA-256) the release lists, and carry the same YaqaLab signature as this program. If anything differs, it is deleted and nothing changes.", L"確認では、github.com/yaqalab/clew-tools に最新バージョンの番号を問い合わせるだけです。あなた自身、フォルダー、録音についての情報は送信されません。GitHub には、どのウェブサイトとも同じく、このコンピューターのインターネットアドレスが見えます。\n\n「インストール」を押すまで、何もダウンロード・インストールされません。新しいインストーラーは、リリースに記載されたサイズと指紋（SHA-256）に完全に一致し、このプログラムと同じ YaqaLab の署名を持つ必要があります。少しでも違えば削除され、何も変わりません。"},
    {S::UpdInstallConfirm, L"Install version %s now?\n\nThe audio server and dictation stop for a moment (usually under a minute) and start again by themselves. Your settings are kept.", L"バージョン %s を今すぐインストールしますか？\n\nオーディオサーバーと音声入力は一時的に停止し（通常 1 分以内）、自動で再開します。設定はそのまま残ります。"},
    {S::UpdBalloonTitle, L"A new version of the Clew audio server", L"Clew オーディオサーバーの新しいバージョン"},
    {S::UpdBalloonText, L"Version %s can be installed from the settings, Updates tab.", L"バージョン %s を設定の「更新」タブからインストールできます。"},
    {S::MenuUpdate, L"Install version %s…", L"バージョン %s をインストール…"},
};
// clang-format on

// Windows' display language, unless CLEW_AUDIO_LANG says "en" or "ja".
bool japanese() {
    static const bool ja = [] {
        wchar_t forced[8] = {};
        if (GetEnvironmentVariableW(L"CLEW_AUDIO_LANG", forced, 8) > 0) return wcscmp(forced, L"ja") == 0;
        return PRIMARYLANGID(GetUserDefaultUILanguage()) == LANG_JAPANESE;
    }();
    return ja;
}

}  // namespace

const wchar_t* tr(S id) {
    for (const auto& p : kStrings)
        if (p.id == id) return japanese() ? p.ja : p.en;
    return L"?";
}

std::wstring trf(S id, ...) {
    wchar_t buf[1024];
    va_list args;
    va_start(args, id);
    _vsnwprintf_s(buf, _countof(buf), _TRUNCATE, tr(id), args);
    va_end(args);
    return buf;
}

}  // namespace clew::win
