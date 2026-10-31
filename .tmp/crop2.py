from PIL import Image
img = Image.open(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\chrome_doubao.png")
w, h = img.size
# cards row: roughly y 480..640 in original
c = img.crop((0, 440, w, 700))
c = c.resize((c.width * 2, c.height * 2), Image.LANCZOS)
c.save(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\crop_cards.png")
# welcome heading area y 300..460
c2 = img.crop((0, 250, w, 460))
c2 = c2.resize((c2.width * 2, c2.height * 2), Image.LANCZOS)
c2.save(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\crop_heading.png")
