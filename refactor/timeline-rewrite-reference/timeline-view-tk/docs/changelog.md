[Project home page](../) > Changelog

------------------------------------------------------------------------

## Changelog


---


### Version 0.10.1

- Updated online help URL.


### Version 0.10.0

- Updated the tlv library to nv_tlview 5.11.2, supporting event colors.


### Version 0.9.1

- Updated the tlv library to nv_tlview 5.6.1, fixing a bug 
  where the scale is not adjusted when maximizing the window. 


### Version 0.9.0

- Updated the tlv library to nv_tlview 5.6.0, refactoring the code 
  and fixing a regression from version 5.5.0 where the minor scale units may not be set the right way. 


### Version 0.8.2

- Updated the tlv library to nv_tlview 5.5.1, preventing the small-size overview from shrinking. 


### Version 0.8.1

- Adjusted the overview spacing ratio.


### Version 0.8.0

- Providing a small-scale overview at the bottom.


### Version 0.7.1

- Fixed a bug where the "shift" indicator line stays visible when releasing
  the mouse button without having changed the position or duration.
- Refactored the code for better performance.


### Version 0.7.0

Refactored the code for better maintainability.
- Renamed the nvtlv package to tlv according to nv_tlv 5.4.0
- Separated the application-specific key definitions.


### Version 0.6.1

- Fixed a bug where the path of an unreadable file may be stored as "last_open". 


### Version 0.6.0

- Refactored the code for better maintainability.


### Version 0.5.1

- Prepared the help URL for translation.
- Updated the help page.


### Version 0.5.0

- Moved the translation tools to the language pack template [tlviewer_de](https://github.com/peter88213/tlviewer_xx). Closes #9.
- Moved the German translations to the separate language pack [tlviewer_de](https://github.com/peter88213/tlviewer_de). Closes #9


### Version 0.4.4

- No longer creating a "temp" directory on startup.


### Version 0.4.3

Added a window icon.


### Version 0.4.2

- Updated the *nvtlview* library to *nv_tlv* 5.3.0.


### Version 0.4.1

- Fixed a regression from 0.4.0 where the "About" dialog does not show up.


### Version 0.4.0

- Renamed the "Substitutions" submenu to "Options" and placed it in the new "Tools" menu.
- New option: "Large toolbar icons".


### Version 0.3.0

- The configuration is persistent now. Closes #2
- Loading the last file on startup. Closes #3
- Updated the *nvtlview* library to *nv_tlv* 5.2.5.


### Version 0.2.0

- Reading and writing csv utf-8 encoded.
- Showing the modification status in the path bar.
- Before changing or closing the project, ask for saving changes. Closes #1
- Moved the "Open project file' command to the "File" menu.


### Version 0.1.0

Alpha version based on the *nvtlview* library version 5.2.4.

To do: Before changing or closing the project, ask for saving changes.