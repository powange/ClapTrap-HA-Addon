# Utilisation de l'image de base Debian pour bénéficier de GCC et de python3-dev et libc-dev
FROM python:3.10.12

# Définition du répertoire de travail
WORKDIR /usr/src/app

COPY data/ ./
RUN apt-get update
RUN apt-get install libasound-dev libportaudio2 libportaudiocpp0 portaudio19-dev python3-opencv ffmpeg libasound2-plugins -y
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 6980
EXPOSE 16045

CMD [ "python", "./app.py" ]