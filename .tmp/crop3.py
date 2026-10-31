from PIL import Image
img = Image.open(r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\chrome_doubao.png")
w, h = img.size
for i, (x0, x1) in enumerate([(0, 950), (950, 1900), (1900, w)]):
    c = img.crop((x0, 420, x1, 720))
    c = c.resize((c.width * 3, c.height * 3), Image.LANCZOS)
    c.save(rf"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\card{i}.png")
