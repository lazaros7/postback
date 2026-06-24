from flask import Flask, request
import os

app = Flask(**name**)

@app.route("/")
def home():
return "Postback Server Running"

@app.route("/postback")
def postback():

```
subid = request.args.get("subid")
reward_event_type = request.args.get("reward_event_type")

print(
    f"POSTBACK RECEIVED | subid={subid} | event={reward_event_type}"
)

return "OK", 200
```

if **name** == "**main**":
app.run(
host="0.0.0.0",
port=int(os.environ.get("PORT", 5000))
)
