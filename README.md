**Instructions:**

Run the python script and it will

Find Telegram.exe in %appdata%\Roaming\Telegram Desktop\ (or ask you for the folder)

Close Telegram if it's running

Show the current patch status

Let you pick a new width from the menu (800/1000/1200/1500/2000px)

To undo: python telegram_bubble_patcher.py --restore

**Note:** `--restore` refuses to run when the `.bak` backup was taken from a
different Telegram version (sizes differ), because restoring it would
downgrade Telegram instead of just undoing the patch. Delete the `.bak`
yourself if you actually want the old version back.

**Telegram 7.x compatibility:** verified working on Telegram Desktop
7.2.9 (x64). The patcher locates the bubble-width writer by a wildcarded
call-sequence signature (the `st::msgMaxWidth` setup block), so small
code-layout changes between versions don't break it. After patching,
restart Telegram completely for the new width to take effect.

<img width="837" height="644" alt="image" src="https://github.com/user-attachments/assets/90719035-b63b-4ee7-b261-2c6bae30eacb" />




**MANUAL METHOD:**

If you want to learn how to do it manually see my blog post https://techstat.net/telegram-windows-app-expand-chat-bubble-size-fix/
