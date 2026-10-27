#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef StageDir
  #error StageDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
[Setup]
AppId={{74DCCB58-21F5-4BB0-B93D-7DF3C76D64F5}
AppName=PhotoMesh Studio
AppVersion={#AppVersion}
AppPublisher=Behdad Nayyerpour
DefaultDirName={localappdata}\Programs\PhotoMesh Studio
DefaultGroupName=PhotoMesh Studio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
SetupIconFile={#StageDir}\app\photomesh\resources\photomesh.ico
OutputDir={#OutputDir}
OutputBaseFilename=PhotoMeshStudio_Setup_{#AppVersion}
UninstallDisplayIcon={app}\app\photomesh\resources\photomesh.ico
Uninstallable=not PortableCheck
UsePreviousAppDir=not PortableCheck
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{#StageDir}\app\*"; DestDir: "{app}\app"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#StageDir}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#StageDir}\tools\*"; DestDir: "{app}\tools"; Flags: recursesubdirs createallsubdirs ignoreversion
[InstallDelete]
Type: filesandordirs; Name: "{app}\app\photomesh"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\tools"

[Icons]
Name: "{autoprograms}\PhotoMesh Studio"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\main.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\photomesh\resources\photomesh.ico"; Check: not PortableCheck
Name: "{autodesktop}\PhotoMesh Studio"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\main.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\photomesh\resources\photomesh.ico"; Tasks: desktopicon; Check: not PortableCheck

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\main.py"""; WorkingDir: "{app}\app"; Description: "Launch PhotoMesh Studio"; Flags: postinstall nowait skipifsilent

[Code]
function PortableCheck: Boolean;
begin
  Result := ExpandConstant('{param:portable|0}') = '1';
end;

