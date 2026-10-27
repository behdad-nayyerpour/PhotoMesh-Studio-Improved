#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef StageDir
  #error StageDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef RuntimeId
  #error RuntimeId is required
#endif
#ifndef UpdateOnly
  #define UpdateOnly "0"
#endif
#ifndef IncludeRuntimeInUpdate
  #define IncludeRuntimeInUpdate "0"
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
#if UpdateOnly == "1"
OutputBaseFilename=PhotoMeshStudio_Update_{#AppVersion}
#else
OutputBaseFilename=PhotoMeshStudio_Setup_{#AppVersion}
#endif
UninstallDisplayIcon={app}\app\photomesh\resources\photomesh.ico
Uninstallable=not PortableCheck
UsePreviousAppDir=not PortableCheck
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{#StageDir}\app\*"; DestDir: "{app}\app"; Flags: recursesubdirs createallsubdirs ignoreversion
#if (UpdateOnly == "0") || (IncludeRuntimeInUpdate == "1")
Source: "{#StageDir}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#StageDir}\tools\*"; DestDir: "{app}\tools"; Flags: recursesubdirs createallsubdirs ignoreversion
#endif

#if UpdateOnly == "0"
[Icons]
Name: "{autoprograms}\PhotoMesh Studio"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\main.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\photomesh\resources\photomesh.ico"; Check: not PortableCheck
Name: "{autodesktop}\PhotoMesh Studio"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\main.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\photomesh\resources\photomesh.ico"; Tasks: desktopicon; Check: not PortableCheck

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\main.py"""; WorkingDir: "{app}\app"; Description: "Launch PhotoMesh Studio"; Flags: postinstall nowait skipifsilent
#endif

[Code]
function PortableCheck: Boolean;
begin
  Result := ExpandConstant('{param:portable|0}') = '1';
end;

#if UpdateOnly == "1"
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  InstalledId: AnsiString;
  IdPath: String;
begin
  Result := '';
  IdPath := ExpandConstant('{app}\app\runtime.id');
  if not FileExists(ExpandConstant('{app}\python\pythonw.exe')) or
     not LoadStringFromFile(IdPath, InstalledId) then
  begin
    Result := 'PhotoMesh Studio is not installed here. Run the full installer first.';
    Exit;
  end;
#if IncludeRuntimeInUpdate == "0"
  if Trim(String(InstalledId)) <> '{#RuntimeId}' then
    Result := 'The installed Python environment differs from this update. Run the full installer for version {#AppVersion}.';
#endif
end;
#endif
