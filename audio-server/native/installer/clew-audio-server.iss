; The installer for the Clew audio server (Inno Setup 6). Built by installer\build-installer.cmd.
;
; Per user, no administrator rights: the program goes to %LOCALAPPDATA%\Programs\Clew Audio Server,
; a Start menu entry, and optionally starts with Windows (the same registry value the settings
; window's "Start with Windows" uses). Uninstalling quits the running program and removes it; the
; settings (folder, port, sealed secret) stay in %APPDATA%\clew-audio-server unless the user says to
; remove them. Converted copies live in the audio folder itself (.clew-converted) and are left there:
; they belong with the recordings.
;
; Dictation (docs/28) is an optional component: the speech server, tools/stt-server's build
; (about 200 MB), in {app}\clew-stt-server, which the tray finds there. No model is shipped; the
; Dictation tab downloads one to %LOCALAPPDATA%\clew-stt\models (or a folder the user chose).

; The version comes from tools/VERSION: build-installer.cmd passes it as /DAppVersion=.
#ifndef AppVersion
  #error AppVersion is not defined: build with build-installer.cmd
#endif
#define AppExe "clew-audio-server.exe"

[Setup]
AppId={{6F3C1E2A-8B4D-4C9E-9A57-2D1B7E0C4F93}
AppName=Clew Audio Server
AppVersion={#AppVersion}
AppVerName=Clew Audio Server {#AppVersion}
AppPublisher=YaqaLab
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\Clew Audio Server
DefaultGroupName=Clew
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=clew-audio-server-setup-{#AppVersion}
SetupIconFile=..\res\icons\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName=Clew Audio Server
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Signed with the YaqaLab certificate when build-installer.cmd finds one (it defines Sign and the
; "yaqalab" sign tool); the uninstaller is signed too.
#ifdef Sign
SignTool=yaqalab
SignedUninstaller=yes
#endif
; Close a running copy before files are replaced (it answers the session-end messages).
CloseApplications=yes
RestartApplications=no
AppMutex=Local\ClewAudioServer.SingleInstance

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "ja"; MessagesFile: "compiler:Languages\Japanese.isl"

[CustomMessages]
en.AutostartTask=Start the audio server with Windows
ja.AutostartTask=Windows の起動時にオーディオサーバーを開始する
en.StartNow=Start the Clew audio server now
ja.StartNow=Clew オーディオサーバーを今すぐ開始する
en.DictationComponent=Dictation on this computer (speech recognition, about 200 MB; models are downloaded later)
ja.DictationComponent=このコンピューターでの音声入力（音声認識、約 200 MB。モデルは後でダウンロード）
en.TypeFull=Audio server and dictation
ja.TypeFull=オーディオサーバーと音声入力
en.TypeCompact=Audio server only
ja.TypeCompact=オーディオサーバーのみ
en.TypeCustom=Choose
ja.TypeCustom=選択する
en.MainComponent=Audio server
ja.MainComponent=オーディオサーバー
en.RemoveModels=Also remove the dictation models downloaded to %1?%n%nThey can be large; keep them if you will install dictation again.
ja.RemoveModels=%1 にダウンロードした音声入力モデルも削除しますか？%n%nサイズが大きい場合があります。音声入力を再インストールする予定がある場合は残してください。
en.RemoveSettings=Also remove the settings (the chosen folder, the port and the secret)?%n%nKeep them if you will install the audio server again: Clew can then go on using the same secret.
ja.RemoveSettings=設定（選択したフォルダー、ポート、シークレット）も削除しますか？%n%n再インストールする予定がある場合は残してください。Clew は同じシークレットを使い続けられます。

[Types]
Name: "full"; Description: "{cm:TypeFull}"
Name: "compact"; Description: "{cm:TypeCompact}"
Name: "custom"; Description: "{cm:TypeCustom}"; Flags: iscustom

[Components]
Name: "main"; Description: "{cm:MainComponent}"; Types: full compact custom; Flags: fixed
Name: "dictation"; Description: "{cm:DictationComponent}"; Types: full

[Tasks]
Name: "autostart"; Description: "{cm:AutostartTask}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\build\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; DestName: "README.txt"; Flags: ignoreversion
Source: "..\..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\..\..\stt-server\dist\clew-stt-server\*"; DestDir: "{app}\clew-stt-server"; Components: dictation; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Dictation unticked on an update: the old copy goes, so the tray does not offer it.
Type: filesandordirs; Name: "{app}\clew-stt-server"; Components: not dictation

[Icons]
Name: "{group}\Clew Audio Server"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,Clew Audio Server}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Clew Audio Server"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ClewAudioServer"; ValueData: """{app}\{#AppExe}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:StartNow}"; Flags: nowait postinstall skipifsilent
; An update started from the program's Updates tab (/SILENT /update=1, docs/32): the program quit to
; let its files be replaced, so the new version starts again by itself.
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: IsUpdate

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--quit"; RunOnceId: "QuitServer"; Flags: runhidden waituntilterminated

[Code]
function IsUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:update|0}') = '1');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // The "Start with Windows" value may have been set from the window, not by this installer.
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'ClewAudioServer');
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then begin
    if DirExists(ExpandConstant('{localappdata}\clew-stt\models')) then
      if MsgBox(FmtMessage(CustomMessage('RemoveModels'), [ExpandConstant('{localappdata}\clew-stt\models')]),
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(ExpandConstant('{localappdata}\clew-stt'), True, True, True);
    if MsgBox(CustomMessage('RemoveSettings'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{userappdata}\clew-audio-server\settings.json'), False, True, False);
  end;
end;
