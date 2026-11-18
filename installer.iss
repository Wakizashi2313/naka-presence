; Installateur NAKA Presence (Inno Setup 6.7) : thème sombre, sans droits administrateur,
; désinstallation sans trace.
#define AppName "NAKA Presence"
#define AppExe "NakaPresence.exe"
; version transmise par build.ps1 (APP_VERSION de naka_presence.py)
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1B7C52-3D0E-4A59-9B5A-1A2B3C4D5E6F}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=NAKA
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
DisableWelcomePage=no
OutputDir=dist
OutputBaseFilename=NakaPresence-Setup
SetupIconFile=naka.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
; l'application est fermée par [Code] (plus rapide et fiable que le Restart Manager)
CloseApplications=no
; même thème que le menu : sombre, sans lignes de séparation, barre de titre sombre
WizardStyle=modern dark windows11 hidebevels includetitlebar
WizardBackColor=#1c1c21
WizardImageFile=wizard.bmp
WizardSmallImageFile=wizard_small.bmp
WizardImageBackColor=#1c1c21
WizardSizePercent=110

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Messages]
french.WelcomeLabel2=Ce programme va installer [name] sur votre ordinateur.%n%nVotre profil Discord affichera la présence NAKA tant que l'application tourne. Elle se pilote depuis l'icône près de l'horloge.

[Tasks]
Name: "autostart"; Description: "Démarrer avec Windows"

[Files]
Source: "dist\NakaPresence\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"

[Registry]
; Valeur de démarrage : créée si la tâche est cochée, toujours supprimée à la désinstallation
; (l'application peut aussi la créer depuis son menu).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "NakaPresence"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: autostart
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "NakaPresence"; Flags: dontcreatekey uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Lancer {#AppName}"; Flags: nowait postinstall skipifsilent
; mise à jour lancée depuis l'application (mode silencieux) : on la relance
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: WizardSilent

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\NakaPresence"
Type: filesandordirs; Name: "{app}"

[Code]
procedure KillApp();
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE,
       ewWaitUntilTerminated, Code);
  Sleep(400);
end;

function AppRunning(): Boolean;
var
  Code: Integer;
begin
  { détecte aussi les anciennes versions (sans mutex) }
  Result := Exec(ExpandConstant('{cmd}'),
    '/C tasklist /NH /FI "IMAGENAME eq {#AppExe}" | find /I "{#AppExe}"', '', SW_HIDE,
    ewWaitUntilTerminated, Code) and (Code = 0);
  Log(Format('AppRunning: code=%d', [Code]));
end;

{ On ferme nous-mêmes l'application : le Restart Manager restait bloqué longtemps. }
function InitializeSetup(): Boolean;
begin
  Result := True;
  if AppRunning() then
  begin
    if SuppressibleMsgBox('NAKA Presence est en cours d''exécution. Le fermer pour continuer l''installation ?',
              mbConfirmation, MB_YESNO, IDYES) = IDYES then
      KillApp()
    else
      Result := False;
  end;
end;

{ Fermeture seulement après la confirmation de l'utilisateur (sinon « Non » fermait l'appli).
  Sans /T : le désinstalleur, lancé depuis le menu de l'appli, est son enfant et serait tué avec. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    KillApp();
end;
