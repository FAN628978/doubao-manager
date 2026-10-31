from PIL import Image
img = Image.open(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\chrome_doubao.png")
w, h = img.size
c = img.crop((0, 0, 360, 260))
c = c.resize((c.width * 2, c.height * 2), Image.LANCZOS)
c.save(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\side_top2.png")
c2 = img.crop((0, 240, 360, 580))
c2 = c2.resize((c2.width * 2, c2.height * 2), Image.LANCZOS)
c2.save(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\side_mid2.png")
