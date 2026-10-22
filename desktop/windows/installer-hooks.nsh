; VoiceCut's additions to Tauri's NSIS installer (tauri.windows.conf.json).

; .vcproj files (a project's launcher, <name>.vc\<name>.vcproj) get the
; document icon instead of the app's: Tauri registers the file type with the
; icon inside VoiceCut.exe.
!macro NSIS_HOOK_POSTINSTALL
  WriteRegStr SHCTX "Software\Classes\VoiceCut.Project\DefaultIcon" "" "$\"$INSTDIR\VoiceCutDocument.ico$\""
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
!macroend
