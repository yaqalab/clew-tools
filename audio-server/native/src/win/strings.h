// Every string the window and the tray show, in English and Japanese. Japanese when Windows' own
// display language is Japanese, English otherwise.
#pragma once

#include <string>

namespace clew::win {

enum class S {
    AppName,
    TipRunning,     // %d port
    TipConverting,  // %s name
    TipStopped,
    TipProblem,  // %s
    MenuStatusRunning,
    MenuStatusStopped,
    MenuOpen,
    MenuCopySecret,
    MenuCopyAddress,
    MenuOpenFolder,
    MenuStop,
    MenuStart,
    MenuAutostart,
    MenuQuit,
    StatusRunning,  // %d port, %llu requests
    StatusStopped,
    StatusProblem,  // %s
    ProblemNoFolder,
    ProblemPortInUse,  // %d
    ProblemFolderGone,  // %s folder
    GroupFolder,
    Browse,
    Open,
    GroupConnection,
    Port,
    LinkHours,
    Origins,
    GroupSecret,
    SecretHint,
    Show,
    Hide,
    Copy,
    NewSecret,
    NewSecretConfirm,
    GroupConversion,
    Convert,
    Prepare,
    Ffmpeg,
    FfmpegFind,
    FfmpegHint,
    FfmpegNotFound,
    CacheLimit,
    CacheUsed,  // %s
    ClearCache,
    Autostart,
    Activity,
    Save,
    Close,
    Saved,
    Copied,
    BadPort,
    BadHours,
    BadCache,
    BadOrigin,  // %s
    BadFolder,
    BadFfmpeg,
    ChooseFolder,
    ChooseFfmpeg,
    FirstRun,
    AlreadyRunning,
    Start,
    Stop,
    // The tabs, and the Dictation tab (docs/28)
    TabAudio,
    TabDictation,
    SttOff,
    SttMissing,
    SttNoModel,   // %s model
    SttLoading,   // %s model
    SttReady,     // %s model, %s languages
    SttFailed,    // %s why
    SttDownloading,     // %s model, %d percent
    SttDownloadFailed,  // %s why
    SttNoMemory,
    LangChosen,
    LangEnglish,
    LangJapanese,
    LangFrench,
    LangMalagasy,
    LangPerSession,
    GroupSttModel,
    SttModelLine,  // %s name, %s size, %s use, %s here or not
    SttUseLive,
    SttUseFiles,
    SttUseGpu,
    SttEnglishOnly,
    SttNeedsMemory,  // %s model, %u GB, %s this computer's
    SttNeedsGpu,     // %s model
    SttGpuFound,
    SttSpeakersMissing,  // %s size
    SttSpeakerModel,
    SttHere,
    SttNotHere,
    SttDownload,
    SttDownloadConfirm,  // %s model, %s size, %s folder, %s free
    SttNoSpace,          // %s needed, %s free
    GroupSttFolder,
    Change,
    SttFree,  // %s
    GroupSttConn,
    Language,
    GroupSttKey,
    SttKeyHint,
    NewSttKeyConfirm,
    SttAutostart,
    SttLocal,
    ChooseModelsFolder,
    BadSttPort,
    NewKey,
    MenuDictationStart,
    MenuDictationStop,
    MenuCopySttKey,
    // Several folders (docs/22 section 11).
    MenuFolders,
    MenuOpenOneFolder,
    FolderAdd,
    FolderRemove,
    FolderRemoveConfirm,  // %s folder
    FolderAlready,
    ColFolderName,
    ColFolderPath,
    ProblemAllOff,
    // New versions (docs/32).
    TabUpdates,
    UpdVersion,  // %s version
    GroupUpd,
    UpdAuto,
    UpdCheck,
    UpdNotes,
    UpdInstall,  // %s version
    UpdInstallNone,
    UpdIdle,
    UpdChecking,
    UpdCurrent,
    UpdAvailable,    // %s version
    UpdDownloading,  // %s version, %d percent
    UpdFailed,       // %s why
    UpdUnsigned,
    UpdKept,
    GroupUpdHow,
    UpdHow,
    UpdInstallConfirm,  // %s version
    UpdBalloonTitle,
    UpdBalloonText,  // %s version
    MenuUpdate,      // %s version
};

const wchar_t* tr(S id);
// tr() with printf-style arguments.
std::wstring trf(S id, ...);

}  // namespace clew::win
