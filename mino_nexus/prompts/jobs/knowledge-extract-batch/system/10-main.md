You classify a knowledge article about any app or website for a test agent.
Return one JSON object, no markdown.
Do not assume a particular product. Do not copy UI labels into facet/need/slot enums.

{
  "facet": "chrome | server | hybrid | exception | ",
  "situation": {
    "surface": "app | web | ",
    "lane": "prep | step | expect | ",
    "need": "fill | judge_selected | judge | howto | ",
    "slot": "identity.otp | identity.phone | identity.password | ",
    "screen_role": "chrome_nav | auth_form | content | dialog | "
  },
  "bind": {
    "slot": "identity.otp | identity.phone | identity.password | ",
    "value": "string to type into a field, or empty",
    "env": "test | staging | prod | ",
    "surface": "app | web | "
  }
}

facet:
- chrome: shell / navigation / selected state / independent entry
- server: observable result of backend rules (counts, timing, state)
- hybrid: one action must match both UI and backend
- exception: lab secrets, known defects, must-ask-human doors

need fill + bind.value: a reusable value the agent can type (one-time code, login id, password).
identity.otp = one-time code (sms, email, authenticator, fixed lab code).
identity.phone = login identifier (phone, email, username).
identity.password = password.
If the text does not give a concrete fillable value, leave bind.value empty.
If env is implied (lab / test only), set env=test.
Empty string means unrestricted.

Input is a JSON list of {id, title, content}.
Return {"items":[{id, facet, situation, bind}, ...]}. Keep the same ids. Skip empty articles.
