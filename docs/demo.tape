# Regenerate with:  vhs docs/demo.tape
# Needs the stack running: make infra && make worker && make api

Output docs/demo.gif

Set Shell bash
Set FontSize 13
Set Width 1100
Set Height 940
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

Hide
Type "cd $(git rev-parse --show-toplevel) && clear"
Enter
Sleep 1s
Show

Type "./scripts/demo.sh"
Sleep 600ms
Enter
Sleep 9s
Sleep 3s
