[Home](../..) > Timeline viewer help

---

# Timeline viewer help

This page refers to the latest
[timeline-view-tk](https://codeberg.org/peter88213/timeline-view-tk/) release. You can
open it with **Help > Online help**.


## Setting up the program 

After downloading and installing the program as described on the download page, 
you might want to make some [preparations](preparations.md) in order to make
it accessible on your desktop. 
 

## Operation

### Launching the application

- You can start the timeline viewer by dragging and dropping a *.csv *file 
  onto the desktop shortcut, then this project will be opened. 
- If you start the timeline viewer via clicking or double-clicking on the
  program icon, the project from the last session will be automatically reopened, 
  if there is one.
- Otherwise a blank timeline view appears, and you can click on the 
  **File > Open** menu item to load an existing project file. 
- In case there isn't one, you may want to click on **File > New** to create
  a csv file. 
  Then you can edit this file with your standard application for csv files, 
  which may be a spreadsheet application, and enter your events.  


### Mouse scrolling

-   Scroll the timeline horizontally with `Shift`-`Mousewheel`.
-   Scroll the timeline vertically with the mousewheel.
-   Scroll the timeline in any direction by right-clicking on the canvas
    and dragging the mouse.
-   Increase or reduce the time scale with `Ctrl`-`Mousewheel`.
-   Change the distance limits for stacking with
    `Shift`-`Ctrl`-`Mousewheel`.


### Shifting an event in time

-   Hold down the `Shift` button and click on the event marker, then
    drag it with the mouse. This will move the event forward or
    backward in time while keeping the duration.

### Shifting the event end

-   Hold down the `Ctrl` and `Shift` buttons and click on the event
    marker, then drag it with the mouse. This will increase or decrease
    the event's duration while keeping the start date/time.

---

**Hint** 

- Shifting operations with the mouse can be aborted with the `Esc` key
before releasing the mouse button. 
- Shifting operations with the mouse can be undone with ![undo](images/undo.png).

---

## Command reference

- ["File" menu](file_menu.md)
- ["Go to" menu](go_to_menu.md)
- ["Scale" menu](scale_menu.md)
- ["Cascading" menu](cascading_menu.md)
- ["Tools" menu](tools_menu.md)
- ["Help" menu](help_menu.md)
- [The footer toolbar](toolbar.md)

