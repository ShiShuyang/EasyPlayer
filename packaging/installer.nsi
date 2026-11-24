Target amd64-unicode

!include "MUI2.nsh"
!include "x64.nsh"
!include "StrFunc.nsh"
${StrRep}

!define APP_NAME "简放"
!define APP_EXE "${APP_PRODUCT}.exe"
!define SHELL_EXE "${APP_PRODUCT}Shell.exe"
!define PLAYLIST_CLSID "{5D96B5DA-F190-4BAC-A8D2-C28237F2489C}"
!define PLAYLIST_VERB "${APP_PRODUCT}.Enqueue"
!define APP_REGISTRY "Software\${APP_PRODUCT}"
!define CAPABILITIES "${APP_REGISTRY}\Capabilities"
!define PREVIOUS_ASSOCIATIONS "${APP_REGISTRY}\PreviousAssociations"
!define UNINSTALL_REGISTRY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_PRODUCT}"
Var AudioProgId
Var VideoProgId

Var RegisterAudio
Var RegisterVideo
Var AudioCheckbox
Var VideoCheckbox
Var ExistingDir
Var StageDir

Name "${APP_NAME} ${APP_VERSION}"
OutFile "${OUTPUT_EXE}"
InstallDir "$PROGRAMFILES64\${APP_PRODUCT}"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
AllowSkipFiles off

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName" "${APP_PRODUCT}"
VIAddVersionKey /LANG=2052 "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey /LANG=2052 "FileDescription" "${APP_NAME}安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright" ""

!define MUI_ICON "${APP_ICON}"
!define MUI_UNICON "${APP_ICON}"
!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipDirectoryOnUpgrade
!insertmacro MUI_PAGE_DIRECTORY
Page custom ShowAssociationOptions SaveAssociationOptions
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TEXT "简放已安装。"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

; Keep a second setup/uninstall from switching ownership between the check and cleanup.
!macro LockSetup
    System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Global\${APP_PRODUCT}.Setup") p.r0 ?e'
    Pop $1
    ${If} $0 == 0
    ${OrIf} $1 == 183
        MessageBox MB_ICONSTOP|MB_OK "安装或卸载程序正在运行，请稍后重试。" /SD IDOK
        SetErrorLevel 2
        Quit
    ${EndIf}
    ; Windows closes the handle when this process exits.
!macroend

Function .onInit
    ${IfNot} ${IsNativeAMD64}
        MessageBox MB_OK|MB_ICONSTOP "此安装包仅支持 Windows x64。"
        Abort
    ${EndIf}
    SetRegView 64
    SetShellVarContext all
    !insertmacro LockSetup
    ReadRegStr $0 HKLM "${UNINSTALL_REGISTRY}" "InstallLocation"
    ${If} $0 != ""
    ${AndIf} ${FileExists} "$0\${APP_EXE}"
    ${AndIf} ${FileExists} "$0\Uninstall.exe"
        GetFullPathName $ExistingDir "$0\."
        StrCpy $INSTDIR $ExistingDir
    ${EndIf}
    StrCpy $RegisterAudio ${BST_CHECKED}
    StrCpy $RegisterVideo ${BST_CHECKED}
FunctionEnd

Function SkipDirectoryOnUpgrade
    ${If} $ExistingDir != ""
        Abort
    ${EndIf}
FunctionEnd

; Windows can retain UserChoice after uninstall. Reuse handler IDs on upgrade
; but never reactivate an uninstalled copy's IDs during a fresh install.
!macro LoadAssociationIds
    ReadRegStr $0 HKLM "${APP_REGISTRY}" "AssociationId"
    StrCpy $AudioProgId "${APP_PRODUCT}.A.$0"
    StrCpy $VideoProgId "${APP_PRODUCT}.V.$0"
!macroend

Function InitAssociationIds
    ReadRegStr $0 HKLM "${APP_REGISTRY}" "AssociationId"
    ${If} $0 == ""
        System::Call 'ole32::CoCreateGuid(g .r0) i.r1'
        ${If} $1 != 0
            Abort "无法创建文件关联标识。请重新运行安装程序。"
        ${EndIf}
        StrCpy $0 $0 36 1
        ${StrRep} $1 $0 "-" ""
        ; Keep the product-prefixed ProgIDs within 39 characters.
        StrCpy $1 $1 24
        ClearErrors
        WriteRegStr HKLM "${APP_REGISTRY}" "AssociationId" "$1"
        ${If} ${Errors}
            Abort "无法保存文件关联标识。请重新运行安装程序。"
        ${EndIf}
    ${EndIf}
    !insertmacro LoadAssociationIds
FunctionEnd

Function un.onInit
    SetRegView 64
    SetShellVarContext all
    !insertmacro LockSetup
    Call un.CheckOwner
FunctionEnd

; Resolve existing directories through Windows, including short names and junctions.
Function un.CanonicalDirectory
    Pop $0
    StrCpy $2 ""
    ${If} $0 != ""
        GetFullPathName $0 "$0\."
        System::Call 'kernel32::CreateFileW(w r0, i 0, i 7, p 0, i 3, i 0x02000000, p 0) p.r1'
        ${If} $1 != -1
            System::Call 'kernel32::GetFinalPathNameByHandleW(p r1, w .r2, i ${NSIS_MAX_STRLEN}, i 0) i.r3'
            System::Call 'kernel32::CloseHandle(p r1)'
            ${If} $3 == 0
            ${OrIf} $3 >= ${NSIS_MAX_STRLEN}
                StrCpy $2 ""
            ${EndIf}
        ${EndIf}
    ${EndIf}
    Push $2
FunctionEnd

Function un.CheckOwner
    ReadRegStr $ExistingDir HKLM "${UNINSTALL_REGISTRY}" "InstallLocation"
    Push $ExistingDir
    Call un.CanonicalDirectory
    Pop $4
    Push $INSTDIR
    Call un.CanonicalDirectory
    Pop $5
    ${If} $4 == ""
    ${OrIf} $4 != $5
        ${If} $ExistingDir == ""
            StrCpy $ExistingDir "（无已登记的安装目录）"
        ${EndIf}
        MessageBox MB_ICONSTOP|MB_OK "已停止卸载。当前登记的安装位置：$\r$\n$ExistingDir" /SD IDOK
        SetErrorLevel 3
        Quit
    ${EndIf}
FunctionEnd

Function ShowAssociationOptions
    ${If} $ExistingDir != ""
        Abort
    ${EndIf}
    !insertmacro MUI_HEADER_TEXT "文件关联" "选择要关联的媒体类型。"
    nsDialogs::Create 1018
    Pop $0
    ${If} $0 == error
        Abort
    ${EndIf}

    ${NSD_CreateLabel} 0 0 100% 30u "勾选后为该类的全部文件格式注册简放的打开方式。Windows 中用户明确选择的默认应用仍然优先。"
    Pop $0
    ${NSD_CreateCheckbox} 0 34u 100% 12u "关联所有支持的音频格式"
    Pop $AudioCheckbox
    ${NSD_SetState} $AudioCheckbox $RegisterAudio
    ${NSD_CreateCheckbox} 0 55u 100% 12u "关联所有支持的视频格式"
    Pop $VideoCheckbox
    ${NSD_SetState} $VideoCheckbox $RegisterVideo
    GetFunctionAddress $0 SaveAssociationOptions
    nsDialogs::OnBack $0
    nsDialogs::Show
FunctionEnd

Function SaveAssociationOptions
    ${NSD_GetState} $AudioCheckbox $RegisterAudio
    ${NSD_GetState} $VideoCheckbox $RegisterVideo
FunctionEnd

!macro AudioExtensions Operation
    !insertmacro ${Operation} ".mp3" "$AudioProgId"
    !insertmacro ${Operation} ".flac" "$AudioProgId"
    !insertmacro ${Operation} ".wav" "$AudioProgId"
    !insertmacro ${Operation} ".m4a" "$AudioProgId"
    !insertmacro ${Operation} ".aac" "$AudioProgId"
    !insertmacro ${Operation} ".ogg" "$AudioProgId"
    !insertmacro ${Operation} ".opus" "$AudioProgId"
    !insertmacro ${Operation} ".wma" "$AudioProgId"
    !insertmacro ${Operation} ".aiff" "$AudioProgId"
!macroend

!macro VideoExtensions Operation
    !insertmacro ${Operation} ".mp4" "$VideoProgId"
    !insertmacro ${Operation} ".mkv" "$VideoProgId"
    !insertmacro ${Operation} ".webm" "$VideoProgId"
    !insertmacro ${Operation} ".mov" "$VideoProgId"
    !insertmacro ${Operation} ".avi" "$VideoProgId"
    !insertmacro ${Operation} ".m4v" "$VideoProgId"
    !insertmacro ${Operation} ".ts" "$VideoProgId"
    !insertmacro ${Operation} ".mpeg" "$VideoProgId"
    !insertmacro ${Operation} ".mpg" "$VideoProgId"
    !insertmacro ${Operation} ".wmv" "$VideoProgId"
!macroend

!macro MediaExtensions Operation
    !insertmacro AudioExtensions ${Operation}
    !insertmacro VideoExtensions ${Operation}
!macroend

!macro RegisterAssociation Extension ProgId
    WriteRegStr HKLM "${CAPABILITIES}\FileAssociations" "${Extension}" "${ProgId}"
    WriteRegStr HKLM "Software\Classes\${Extension}\OpenWithProgids" "${ProgId}" ""
!macroend

!macro RestoreAssociation Root Extension ProgId
    ReadRegStr $0 ${Root} "Software\Classes\${Extension}" ""
    ${If} $0 == "${ProgId}"
        ClearErrors
        ReadRegStr $0 ${Root} "${PREVIOUS_ASSOCIATIONS}" "${Extension}"
        ${If} ${Errors}
            DeleteRegValue ${Root} "Software\Classes\${Extension}" ""
        ${Else}
            WriteRegStr ${Root} "Software\Classes\${Extension}" "" "$0"
        ${EndIf}
    ${EndIf}
!macroend

!macro UnregisterAssociation Extension ProgId
    DeleteRegValue HKLM "Software\Classes\${Extension}\OpenWithProgids" "${ProgId}"
    !insertmacro RestoreAssociation HKLM "${Extension}" "${ProgId}"
    !insertmacro RestoreAssociation HKCU "${Extension}" "${ProgId}"
!macroend

!macro SetAssociation Root Extension ProgId
    ClearErrors
    ReadRegStr $0 ${Root} "Software\Classes\${Extension}" ""
    ${If} ${Errors}
        ; Only create a machine fallback. A missing per-user value inherits it.
        !if "${Root}" == "HKLM"
            DeleteRegValue ${Root} "${PREVIOUS_ASSOCIATIONS}" "${Extension}"
            WriteRegStr ${Root} "Software\Classes\${Extension}" "" "${ProgId}"
        !endif
    ${ElseIf} $0 != "${ProgId}"
        ClearErrors
        WriteRegStr ${Root} "${PREVIOUS_ASSOCIATIONS}" "${Extension}" "$0"
        ${IfNot} ${Errors}
            WriteRegStr ${Root} "Software\Classes\${Extension}" "" "${ProgId}"
        ${EndIf}
    ${EndIf}
!macroend

!macro AssociateExtension Extension ProgId
    !insertmacro SetAssociation HKLM "${Extension}" "${ProgId}"
    !insertmacro SetAssociation HKCU "${Extension}" "${ProgId}"
!macroend

!macro RegisterPlaylistExtension Extension ProgId
    WriteRegStr HKLM "Software\Classes\SystemFileAssociations\${Extension}\shell\${PLAYLIST_VERB}" "" "加入简放播放列表"
    WriteRegStr HKLM "Software\Classes\SystemFileAssociations\${Extension}\shell\${PLAYLIST_VERB}" "Icon" '"$INSTDIR\${APP_EXE}",0'
    WriteRegStr HKLM "Software\Classes\SystemFileAssociations\${Extension}\shell\${PLAYLIST_VERB}" "MultiSelectModel" "Player"
    WriteRegStr HKLM "Software\Classes\SystemFileAssociations\${Extension}\shell\${PLAYLIST_VERB}\DropTarget" "CLSID" "${PLAYLIST_CLSID}"
!macroend

!macro UnregisterPlaylistExtension Extension ProgId
    DeleteRegKey HKLM "Software\Classes\SystemFileAssociations\${Extension}\shell\${PLAYLIST_VERB}"
!macroend

!macro RegisterProgId ProgId Description IconFile
    WriteRegStr HKLM "Software\Classes\${ProgId}" "" "${Description}"
    WriteRegStr HKLM "Software\Classes\${ProgId}\DefaultIcon" "" '"$INSTDIR\icons\${IconFile}",0'
    WriteRegStr HKLM "Software\Classes\${ProgId}\shell\open\command" "" '"$INSTDIR\${APP_EXE}" "%1"'
!macroend

; Replace the private runtime as one directory so obsolete plugins disappear.
; Keep the previous files until every rename succeeds (including on a locked EXE).
!macro MoveEntry Source Destination
    StrCpy $6 8
    ${Do}
        System::Call 'kernel32::MoveFileW(w "${Source}", w "${Destination}") i.r7 ?e'
        Pop $8
        ${If} $7 != 0
            ClearErrors
            ${ExitDo}
        ${EndIf}
        IntOp $6 $6 - 1
        ${If} $6 == 0
            SetErrors
            ${ExitDo}
        ${EndIf}
        ${If} $8 != 5
        ${AndIf} $8 != 32
            SetErrors
            ${ExitDo}
        ${EndIf}
        Sleep 150
    ${Loop}
!macroend

Function InstallEntry
    Pop $0
    ClearErrors
    IfFileExists "$INSTDIR\$0" 0 install_new
    !insertmacro MoveEntry "$INSTDIR\$0" "$StageDir\old\$0"
    IfErrors rollback
install_new:
    !insertmacro MoveEntry "$StageDir\new\$0" "$INSTDIR\$0"
    IfErrors 0 installed
    IfFileExists "$StageDir\old\$0" 0 rollback
    !insertmacro MoveEntry "$StageDir\old\$0" "$INSTDIR\$0"
    IfErrors recovery_failed
rollback:
    Pop $0
    StrCmp $0 "" rolled_back
    !insertmacro MoveEntry "$INSTDIR\$0" "$StageDir\new\$0"
    IfErrors recovery_failed
    IfFileExists "$StageDir\old\$0" 0 rollback
    !insertmacro MoveEntry "$StageDir\old\$0" "$INSTDIR\$0"
    IfErrors recovery_failed
    Goto rollback
rolled_back:
    RMDir /r "$StageDir"
    MessageBox MB_ICONSTOP|MB_OK "无法替换程序文件，请关闭简放后重试。" /SD IDOK
    SetErrorLevel 4
    Abort
recovery_failed:
    MessageBox MB_ICONSTOP|MB_OK "恢复旧文件失败。备份保存在 $StageDir\old。" /SD IDOK
    SetErrorLevel 4
    Abort
installed:
    ; Keep the entry on the rollback stack until the full payload is in place.
    Push $0
FunctionEnd

Section "${APP_NAME}" MainSection
    ${If} $ExistingDir != ""
        StrCpy $INSTDIR $ExistingDir
    ${EndIf}
    SetOutPath "$INSTDIR"
    ClearErrors
    GetTempFileName $StageDir "$INSTDIR"
    IfErrors stage_error
    Delete "$StageDir"
    CreateDirectory "$StageDir\old\icons"
    SetOutPath "$StageDir\new"
    IfErrors stage_error
    File /r "${PAYLOAD_DIR}\*.*"
    WriteUninstaller "$StageDir\new\Uninstall.exe"
    IfErrors stage_error
    SetOutPath "$INSTDIR"
    CreateDirectory "$INSTDIR\icons"
    Push ""
    Push "${APP_EXE}"
    Call InstallEntry
    Push "${SHELL_EXE}"
    Call InstallEntry
    Push "${APP_PRODUCT}Runtime"
    Call InstallEntry
    Push "icons\audio.ico"
    Call InstallEntry
    Push "icons\video.ico"
    Call InstallEntry
    Push "Uninstall.exe"
    Call InstallEntry
clear_stack:
    Pop $0
    StrCmp $0 "" files_ready clear_stack
stage_error:
    SetOutPath "$INSTDIR"
    ${If} $StageDir != ""
        RMDir /r "$StageDir"
    ${EndIf}
    SetErrorLevel 4
    Abort "无法暂存安装文件。"
files_ready:
    RMDir /r /REBOOTOK "$StageDir"
    Call InitAssociationIds
    CreateShortcut "$SMPROGRAMS\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
    WriteRegStr HKLM "Software\Classes\CLSID\${PLAYLIST_CLSID}\LocalServer32" "" '"$INSTDIR\${SHELL_EXE}"'

    !insertmacro RegisterProgId "$AudioProgId" "简放音频文件" "audio.ico"
    !insertmacro RegisterProgId "$VideoProgId" "简放视频文件" "video.ico"
    !insertmacro MediaExtensions RegisterPlaylistExtension
    !insertmacro MediaExtensions RegisterAssociation
    WriteRegStr HKLM "${CAPABILITIES}" "ApplicationName" "${APP_NAME}"
    WriteRegStr HKLM "${CAPABILITIES}" "ApplicationDescription" "用于播放本地音乐、视频和 360° 全景视频。"
    WriteRegStr HKLM "${CAPABILITIES}" "ApplicationIcon" '"$INSTDIR\${APP_EXE}",0'
    WriteRegStr HKLM "Software\RegisteredApplications" "${APP_NAME}" "${CAPABILITIES}"

    ${If} $ExistingDir == ""
    ${AndIf} $RegisterAudio == ${BST_CHECKED}
        !insertmacro AudioExtensions AssociateExtension
    ${EndIf}
    ${If} $ExistingDir == ""
    ${AndIf} $RegisterVideo == ${BST_CHECKED}
        !insertmacro VideoExtensions AssociateExtension
    ${EndIf}

    WriteRegStr HKLM "${UNINSTALL_REGISTRY}" "DisplayName" "${APP_NAME}"
    WriteRegStr HKLM "${UNINSTALL_REGISTRY}" "Publisher" "${APP_PUBLISHER}"
    WriteRegStr HKLM "${UNINSTALL_REGISTRY}" "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKLM "${UNINSTALL_REGISTRY}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKLM "${UNINSTALL_REGISTRY}" "DisplayIcon" '"$INSTDIR\${APP_EXE}",0'
    WriteRegStr HKLM "${UNINSTALL_REGISTRY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
    WriteRegDWORD HKLM "${UNINSTALL_REGISTRY}" "NoModify" 1
    WriteRegDWORD HKLM "${UNINSTALL_REGISTRY}" "NoRepair" 1
    SectionGetSize ${MainSection} $0
    WriteRegDWORD HKLM "${UNINSTALL_REGISTRY}" "EstimatedSize" $0
    System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
SectionEnd

Section "Uninstall"
    Call un.CheckOwner
    !insertmacro LoadAssociationIds
    !insertmacro MediaExtensions UnregisterAssociation
    !insertmacro MediaExtensions UnregisterPlaylistExtension
    DeleteRegValue HKLM "Software\RegisteredApplications" "${APP_NAME}"
    DeleteRegKey HKLM "${CAPABILITIES}"
    DeleteRegKey HKLM "${PREVIOUS_ASSOCIATIONS}"
    DeleteRegKey HKCU "${PREVIOUS_ASSOCIATIONS}"
    DeleteRegKey /ifempty HKCU "${APP_REGISTRY}"
    DeleteRegValue HKLM "${APP_REGISTRY}" "AssociationId"
    DeleteRegKey /ifempty HKLM "${APP_REGISTRY}"
    DeleteRegKey HKLM "Software\Classes\$AudioProgId"
    DeleteRegKey HKLM "Software\Classes\$VideoProgId"
    DeleteRegKey HKLM "Software\Classes\CLSID\${PLAYLIST_CLSID}"
    DeleteRegKey HKLM "${UNINSTALL_REGISTRY}"
    Delete "$SMPROGRAMS\${APP_NAME}.lnk"
    Delete /REBOOTOK "$INSTDIR\${APP_EXE}"
    Delete /REBOOTOK "$INSTDIR\${SHELL_EXE}"
    Delete /REBOOTOK "$INSTDIR\icons\audio.ico"
    Delete /REBOOTOK "$INSTDIR\icons\video.ico"
    RMDir /REBOOTOK "$INSTDIR\icons"
    Delete "$INSTDIR\Uninstall.exe"
    RMDir /r /REBOOTOK "$INSTDIR\${APP_PRODUCT}Runtime"
    RMDir /REBOOTOK "$INSTDIR"
    System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
SectionEnd
