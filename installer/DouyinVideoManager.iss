; Douyin Video Manager - bo cai Windows (Inno Setup 6).
; Build:  installer\build_installer.bat   (hoac: ISCC /DAppVersion=1.0.0 installer\DouyinVideoManager.iss)
; Can truoc: dist\DouyinVideoManager.exe (build_windows_py311.bat) va thu muc installer\redist
;            (installer\prepare_redist.bat tu tai ve).

#define AppName "Douyin Video Manager"
#define AppExe  "DouyinVideoManager.exe"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
; GUID co dinh de ban cap nhat ghi de dung ban cu. Doi GUID neu fork thanh app khac.
AppId={{B6F1D3E2-7C41-4A5B-9E0D-3F2A8C1D5E77}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=DouyinVideoManager-Setup-{#AppVersion}
SetupIconFile=..\assets\app_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
; Cai Python/VC++ Runtime cho moi nguoi dung can quyen quan tri.
PrivilegesRequired=admin
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Tạo biểu tượng ngoài Desktop"; Flags: unchecked
Name: "installpython"; Description: "Cài Python 3.12 (cần để dùng giọng đọc VieNeu; bỏ chọn nếu chỉ dùng Gemini TTS)"; Check: NeedPython

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
; ffmpeg + ffprobe đi kèm: app tự tìm trong thư mục này, không cần sửa PATH.
Source: "redist\ffmpeg\*"; DestDir: "{app}\ffmpeg"; Flags: ignoreversion recursesubdirs
; Chỉ giải nén khi thật sự cần (Check) và xóa ngay sau khi cài.
Source: "redist\vc_redist.x64.exe";   DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedVCRedist
Source: "redist\python-installer.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Tasks: installpython

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Gỡ cài đặt {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; \
  StatusMsg: "Đang cài Microsoft Visual C++ Runtime..."; Flags: waituntilterminated; Check: NeedVCRedist
Filename: "{tmp}\python-installer.exe"; \
  Parameters: "/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_pip=1 Include_tcltk=0 Include_test=0 Include_doc=0 Include_dev=0 Include_debug=0"; \
  StatusMsg: "Đang cài Python 3.12 (mất vài phút)..."; Flags: waituntilterminated; Tasks: installpython
Filename: "{app}\{#AppExe}"; Description: "Mở {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Môi trường Python riêng của VieNeu do app tạo ra (không xóa cache model của HuggingFace).
Type: filesandordirs; Name: "{%USERPROFILE}\.douyin_vieneu_venv"

[Code]
// VC++ Runtime 2015-2022 x64 phải từ bản 14.29 trở lên (onnxruntime cần vcruntime140_1.dll).
function NeedVCRedist: Boolean;
var
  Installed, Major, Minor: Cardinal;
begin
  Result := True;
  if RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) and
     RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Major', Major) and
     RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Minor', Minor) then
    Result := not ((Installed = 1) and ((Major > 14) or ((Major = 14) and (Minor >= 29))));
end;

function PythonInstalled(const Ver: String): Boolean;
var
  Key: String;
begin
  Key := 'SOFTWARE\Python\PythonCore\' + Ver + '\InstallPath';
  Result := RegKeyExists(HKLM, Key) or RegKeyExists(HKCU, Key);
end;

// App chấp nhận Python 3.10 trở lên; đã có bản nào thì không cài thêm.
function NeedPython: Boolean;
begin
  Result := not (PythonInstalled('3.10') or PythonInstalled('3.11') or
                 PythonInstalled('3.12') or PythonInstalled('3.13'));
end;
